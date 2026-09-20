#include "mdMcpPanelTools.h"

#include "mdLcdInteractionModel.h"
#include "mdPluginProcessor.h"

#include "mdLib/mddevice.h"
#include "mdLib/mdfrontpanel.h"

#include "mcpServerLib/mcpServer.h"
#include "mcpServerLib/mcpTool.h"

#include <stdexcept>
#include <string>

namespace mdJucePlugin
{
	namespace
	{
		const char* toString(const md::FrontPanel::LedColor _color)
		{
			switch(_color)
			{
			case md::FrontPanel::LedColor::Off:    return "off";
			case md::FrontPanel::LedColor::Green:  return "green";
			case md::FrontPanel::LedColor::Red:    return "red";
			case md::FrontPanel::LedColor::Yellow: return "yellow";
			}
			return "off";
		}

		const char* toString(const lcdInteraction::SurfaceKind _surface)
		{
			switch(_surface)
			{
			case lcdInteraction::SurfaceKind::EditGrid:         return "editGrid";
			case lcdInteraction::SurfaceKind::Lfo:              return "lfo";
			case lcdInteraction::SurfaceKind::MasterFxEcho:     return "masterFxEcho";
			case lcdInteraction::SurfaceKind::MasterFxReverb:   return "masterFxReverb";
			case lcdInteraction::SurfaceKind::MasterFxEq:       return "masterFxEq";
			case lcdInteraction::SurfaceKind::MasterFxDynamics: return "masterFxDynamics";
			}
			return "editGrid";
		}

		const char* toString(const lcdInteraction::LayoutKind _layout)
		{
			switch(_layout)
			{
			case lcdInteraction::LayoutKind::Standard: return "standard";
			case lcdInteraction::LayoutKind::Lfo:      return "lfo";
			case lcdInteraction::LayoutKind::MasterFx: return "masterFx";
			}
			return "standard";
		}

		std::string hexByte(const uint8_t _value)
		{
			static constexpr char digits[] = "0123456789abcdef";
			return std::string("0x") + digits[_value >> 4] + digits[_value & 0xf];
		}

		// One character per LCD pixel, row-major from the top. '#' is lit.
		mcpServer::JsonValue lcdRows(const md::FrontPanel& _panel)
		{
			auto rows = mcpServer::JsonValue::array();
			std::string row;
			row.resize(md::FrontPanel::g_lcdWidth);

			for(uint32_t y=0; y<md::FrontPanel::g_lcdHeight; ++y)
			{
				for(uint32_t x=0; x<md::FrontPanel::g_lcdWidth; ++x)
					row[x] = _panel.getLcdPixel(x, y) ? '#' : '.';
				rows.append(mcpServer::JsonValue::fromString(juce::String(row)));
			}
			return rows;
		}

		mcpServer::JsonValue ledBanks(const md::FrontPanel& _panel)
		{
			auto banks = mcpServer::JsonValue::object();

			for(uint8_t cmd = md::FrontPanel::g_firstLedBank; cmd <= md::FrontPanel::g_lastLedBank; ++cmd)
			{
				auto bank = mcpServer::JsonValue::object();
				bank.set("raw", mcpServer::JsonValue::fromInt(_panel.getLedBankRaw(cmd)));
				bank.set("written", mcpServer::JsonValue::fromBool(_panel.wasLedBankWritten(cmd)));
				banks.set(hexByte(cmd), bank);
			}
			return banks;
		}

		mcpServer::JsonValue steps(const md::FrontPanel& _panel, const md::MachineModel _model)
		{
			auto result = mcpServer::JsonValue::array();

			for(uint32_t i=1; i<=16; ++i)
			{
				if(_model == md::MachineModel::Monomachine)
					result.append(mcpServer::JsonValue::fromString(toString(_panel.getMonomachineStepLedColor(i))));
				else
					result.append(mcpServer::JsonValue::fromBool(_panel.getStepLed(i)));
			}
			return result;
		}

		mcpServer::JsonValue drums(const md::FrontPanel& _panel)
		{
			auto result = mcpServer::JsonValue::array();
			for(uint32_t i=0; i<16; ++i)
				result.append(mcpServer::JsonValue::fromBool(_panel.getDrumLed(i)));
			return result;
		}

		mcpServer::JsonValue statusLeds(const md::FrontPanel& _panel)
		{
			using Led = md::FrontPanel::StatusLed;

			static constexpr std::pair<const char*, Led> g_leds[]
			{
				{"page1",     Led::Page1},
				{"page2",     Led::Page2},
				{"page3",     Led::Page3},
				{"pattern",   Led::Pattern},
				{"song",      Led::Song},
				{"routing",   Led::Routing},
				{"effects",   Led::Effects},
				{"synthesis", Led::Synthesis},
			};

			auto result = mcpServer::JsonValue::object();
			for(const auto& [name, led] : g_leds)
				result.set(name, mcpServer::JsonValue::fromBool(_panel.getStatusLed(led)));
			return result;
		}

		mcpServer::JsonValue modeLeds(const md::FrontPanel& _panel)
		{
			using Led = md::FrontPanel::ModeLed;

			static constexpr std::pair<const char*, Led> g_leds[]
			{
				{"classic",     Led::Classic},
				{"extended",    Led::Extended},
				{"bankGroupAD", Led::BankGroupAD},
				{"bankGroupEH", Led::BankGroupEH},
				{"record",      Led::Record},
				{"tempo",       Led::Tempo},
				{"page4",       Led::Page4},
			};

			auto result = mcpServer::JsonValue::object();
			for(const auto& [name, led] : g_leds)
				result.set(name, mcpServer::JsonValue::fromBool(_panel.getModeLed(led)));
			return result;
		}
	}

	void registerPanelTools(mcpServer::McpServer& _server, AudioPluginAudioProcessor& _processor)
	{
		mcpServer::ToolDef tool;
		tool.name = "get_front_panel";
		tool.description =
			"Get the decoded Elektron MD/MM front panel: LED banks, step/drum/status/mode LEDs, "
			"the classified LCD page, and optionally the 128x64 LCD framebuffer as ASCII rows "
			"('#' = lit pixel). Reads emulated panel state directly, so it works whether or not "
			"the editor window is open.";
		tool.inputSchema.addProperty("lcd", "boolean",
			"Include the 128x64 LCD framebuffer as 64 ASCII rows (default: true)", false);
		tool.inputSchema.addProperty("dataEntrySwitchHeld", "boolean",
			"Classify the LCD page as if a DATA ENTRY encoder switch is held (default: false)", false);

		tool.handler = [&_processor](const mcpServer::JsonValue& _params) -> mcpServer::JsonValue
		{
			const bool wantLcd = !_params.isObject() || !_params.hasProperty("lcd")
				|| _params.get("lcd").getBool();
			const bool switchHeld = _params.isObject() && _params.hasProperty("dataEntrySwitchHeld")
				&& _params.get("dataEntrySwitchHeld").getBool();

			const auto model = _processor.getModel();

			md::FrontPanel panel;
			bool haveDevice = false;
			uint64_t epoch = 0;

			_processor.getPlugin().withDeviceLocked([&](synthLib::Device* const _device)
			{
				auto* const device = dynamic_cast<md::Device*>(_device);
				if(!device)
					return;
				panel = device->getFrontPanelSnapshot();
				epoch = device->hardwareEpoch();
				haveDevice = true;
			});

			if(!haveDevice)
				throw std::runtime_error("No Elektron device instance available (firmware not loaded?)");

			auto result = mcpServer::JsonValue::object();

			result.set("model", mcpServer::JsonValue::fromString(
				model == md::MachineModel::Monomachine ? "Monomachine" : "Machinedrum"));
			result.set("hardwareEpoch", mcpServer::JsonValue::fromInt64(static_cast<int64_t>(epoch)));

			result.set("litPixels", mcpServer::JsonValue::fromInt64(panel.countLitPixels()));
			result.set("panelBytes", mcpServer::JsonValue::fromInt64(panel.getByteCount()));
			result.set("tileWrites", mcpServer::JsonValue::fromInt64(panel.getTileWriteCount()));
			result.set("ledCommands", mcpServer::JsonValue::fromInt64(panel.getLedCommandCount()));

			result.set("ledBanks", ledBanks(panel));
			result.set("steps", steps(panel, model));
			result.set("drums", drums(panel));
			result.set("status", statusLeds(panel));
			result.set("mode", modeLeds(panel));

			if(const auto state = lcdInteraction::classify(panel, model, switchHeld))
			{
				auto page = mcpServer::JsonValue::object();
				page.set("surface", mcpServer::JsonValue::fromString(toString(state->surface)));
				page.set("layout", mcpServer::JsonValue::fromString(toString(state->layout)));
				page.set("activeEncoderMask", mcpServer::JsonValue::fromInt(state->activeEncoderMask));
				page.set("identityToken", mcpServer::JsonValue::fromInt64(
					static_cast<int64_t>(state->identityToken)));
				result.set("page", page);
			}
			else
			{
				result.set("page", mcpServer::JsonValue::null());
			}

			if(wantLcd)
			{
				result.set("lcdWidth", mcpServer::JsonValue::fromInt(md::FrontPanel::g_lcdWidth));
				result.set("lcdHeight", mcpServer::JsonValue::fromInt(md::FrontPanel::g_lcdHeight));
				result.set("lcd", lcdRows(panel));
			}

			return result;
		};

		_server.registerTool(std::move(tool));
	}
}

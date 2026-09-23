// A device that cannot produce state must not have a state restored onto its
// replacement.
//
// Plugin::setDevice migrates the outgoing device's state to the incoming one.
// Plugin::getState pushes a version byte and a state-type byte before asking
// the device, so a device that returns false still leaves two bytes behind --
// and setDevice's "is the state empty?" guard is therefore never true. Those
// two bytes then pass every check in Plugin::setState, get stripped to an
// empty body, and arrive at the new device as a genuine state transaction.
//
// Observable symptom: swapping a firmware-less DummyDevice for a real device
// raised "The Monomachine project payload is invalid or incompatible." on a
// completely fresh launch, with no saved session anywhere.

#include "synthLib/device.h"
#include "synthLib/plugin.h"

#include <cstdint>
#include <iostream>
#include <vector>

namespace
{
	// Mirrors a device that has no state to give: DummyDevice, or any real
	// device whose firmware failed to load.
	class StatelessDevice final : public synthLib::Device
	{
	public:
		StatelessDevice() : Device({}) {}

		float getSamplerate() const override { return 48000.0f; }
		bool isValid() const override { return true; }
		bool getState(std::vector<uint8_t>&, synthLib::StateType) override
		{
			return false;
		}
		bool setState(const std::vector<uint8_t>&, synthLib::StateType) override
		{
			return false;
		}
		uint32_t getChannelCountIn() override { return 0; }
		uint32_t getChannelCountOut() override { return 2; }
		bool setDspClockPercent(uint32_t) override { return true; }
		uint32_t getDspClockPercent() const override { return 100; }
		uint64_t getDspClockHz() const override { return 0; }

	protected:
		void readMidiOut(std::vector<synthLib::SMidiEvent>&) override {}
		void processAudio(const synthLib::TAudioInputs&,
			const synthLib::TAudioOutputs&, size_t) override {}
		bool sendMidi(const synthLib::SMidiEvent&,
			std::vector<synthLib::SMidiEvent>&) override { return true; }
	};

	// Stands in for the real device that replaces it, and records whether a
	// state restore was attempted and what it received.
	class RecordingDevice final : public synthLib::Device
	{
	public:
		RecordingDevice() : Device({}) {}

		float getSamplerate() const override { return 48000.0f; }
		bool isValid() const override { return true; }
		bool getState(std::vector<uint8_t>&, synthLib::StateType) override
		{
			return false;
		}
		bool setState(const std::vector<uint8_t>& _state,
			synthLib::StateType) override
		{
			++m_setStateCalls;
			m_lastSize = _state.size();
			return true;
		}
		uint32_t getChannelCountIn() override { return 0; }
		uint32_t getChannelCountOut() override { return 2; }
		bool setDspClockPercent(uint32_t) override { return true; }
		uint32_t getDspClockPercent() const override { return 100; }
		uint64_t getDspClockHz() const override { return 0; }

		uint32_t setStateCalls() const { return m_setStateCalls; }
		size_t lastSize() const { return m_lastSize; }

	protected:
		void readMidiOut(std::vector<synthLib::SMidiEvent>&) override {}
		void processAudio(const synthLib::TAudioInputs&,
			const synthLib::TAudioOutputs&, size_t) override {}
		bool sendMidi(const synthLib::SMidiEvent&,
			std::vector<synthLib::SMidiEvent>&) override { return true; }

	private:
		uint32_t m_setStateCalls = 0;
		size_t m_lastSize = 0;
	};

	int fail(const char* const _message)
	{
		std::cerr << _message << '\n';
		return 1;
	}
}

int main()
{
	// Plugin takes ownership: setDevice deletes the outgoing device and the
	// destructor deletes the survivor, so these must be heap allocated.
	auto* const stateless = new StatelessDevice();
	synthLib::Plugin plugin(stateless, [](synthLib::Device* const _device)
	{
		return _device;
	});

	// A device that reports no state must produce no state at all, not a bare
	// two-byte header that later looks like a valid payload.
	std::vector<uint8_t> state;
	if(plugin.getState(state, synthLib::StateTypeGlobal))
		return fail("getState succeeded for a device that has no state");
	if(!state.empty())
		return fail("getState left a header behind after the device declined");

	auto* const replacement = new RecordingDevice();
	plugin.setDevice(replacement);

	if(replacement->setStateCalls() != 0)
		return fail("a state restore was attempted onto the replacement device "
			"even though the outgoing device had no state to migrate");

	// Sessions saved before the producing side was fixed still hold a bare
	// header. Restoring one must be a no-op, not a payload the device has to
	// reject as corrupt.
	const std::vector<uint8_t> persistedStub{1, synthLib::StateTypeGlobal};
	if(plugin.setState(persistedStub))
		return fail("a header-only state reported a successful restore");
	if(replacement->setStateCalls() != 0)
		return fail("a header-only state was handed to the device as a payload");

	std::cout << "device swap state test passed\n";
	return 0;
}

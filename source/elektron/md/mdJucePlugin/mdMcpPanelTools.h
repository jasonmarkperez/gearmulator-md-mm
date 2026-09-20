#pragma once

namespace mcpServer
{
	class McpServer;
}

namespace mdJucePlugin
{
	class AudioPluginAudioProcessor;

	// Exposes the decoded Elektron front panel (LCD framebuffer, LED banks and
	// the classified LCD page) over MCP so external test drivers can assert on
	// what the machine displays instead of on screenshots.
	void registerPanelTools(mcpServer::McpServer& _server, AudioPluginAudioProcessor& _processor);
}

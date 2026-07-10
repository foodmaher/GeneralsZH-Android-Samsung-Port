// GeneralsX @feature Android port 10/07/2026 A couple of globals that the
// ported GeneralsOnline interfaces (OnlineServices_LobbyInterface.cpp,
// NGMP_Helpers.cpp) expect to find defined elsewhere -- in upstream
// SuperHackers_GO they live in the desktop WOL-lobby menu source
// (WOLGameSetupMenu.cpp), which this port deliberately does not carry over
// (it's a full rewrite of the legacy .wnd-based multiplayer UI, out of scope
// for this first pass; our own lobby screen, once built, will replace it).
// This file is NOT ported from upstream -- it's our own minimal stand-in so
// the rest of the module links.

#include "GameNetwork/GeneralsOnline/NGMP_interfaces.h"

NGMPGame* TheNGMPGame = nullptr;

void OnKickedFromLobby()
{
	if (TheNGMPGame != nullptr)
	{
		TheNGMPGame->reset();
	}

	NetworkLog(ELogVerbosity::LOG_RELEASE, "[GeneralsOnline] Kicked from lobby");

	// TODO_GO_ANDROID: once a lobby-browser screen exists, this should also
	// navigate the player back out of it (mirrors upstream's nextScreen +
	// TheShell->pop() in WOLGameSetupMenu.cpp's OnKickedFromLobby()).
}

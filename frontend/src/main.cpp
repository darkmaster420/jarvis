#include "BackendLauncher.h"
#include "Hud.h"
#include "State.h"
#include "WsClient.h"

#include <ixwebsocket/IXNetSystem.h>

#include <cstdio>
#include <cstdlib>
#include <string>

#ifdef _WIN32
#  include <windows.h>

extern "C" {
__declspec(dllexport) DWORD NvOptimusEnablement = 0x00000001;
__declspec(dllexport) int AmdPowerXpressRequestHighPerformance = 1;
}
#endif

int main(int argc, char** argv) {
    std::string url         = "ws://127.0.0.1:8765";
    bool        spawn_back  = true;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if ((arg == "--url" || arg == "-u") && i + 1 < argc) {
            url = argv[++i];
        } else if (arg == "--no-backend") {
            spawn_back = false;
        } else if (arg == "--help" || arg == "-h") {
            std::printf("Usage: jarvis [--url ws://host:port] [--no-backend]\n");
            return 0;
        }
    }
    if (const char* env = std::getenv("JARVIS_URL")) {
        url = env;
    }

    ix::initNetSystem();

    jarvis::BackendLauncher backend;
    if (spawn_back) {
        std::string info;
        if (backend.start(info)) {
            std::fprintf(stderr, "[jarvis] %s\n", info.c_str());
        } else {
            std::fprintf(stderr, "[jarvis] backend launch failed: %s\n",
                         info.c_str());
        }
    }

    jarvis::SharedState state;
    jarvis::WsClient    ws(state, url);
    jarvis::Hud         hud(state, ws, backend.logPath());

    int rc = 0;
    if (!hud.init()) {
        std::fprintf(stderr, "HUD init failed\n");
        rc = 1;
    } else {
        ws.start();
        hud.run();
        ws.stop();
        hud.shutdown();
    }
    backend.stop();
    ix::uninitNetSystem();
    return rc;
}

#ifdef _WIN32
int WINAPI WinMain(HINSTANCE, HINSTANCE, LPSTR, int) {
    return main(__argc, __argv);
}
#endif

#ifdef _WIN32
#  include <winsock2.h>
#  include <ws2tcpip.h>
#endif

#include "BackendLauncher.h"
#include "Hud.h"
#include "State.h"
#include "WsClient.h"

#include <ixwebsocket/IXNetSystem.h>

#include <cstdio>
#include <cstdlib>
#include <string>

#ifdef _WIN32

extern "C" {
__declspec(dllexport) DWORD NvOptimusEnablement = 0x00000001;
__declspec(dllexport) int AmdPowerXpressRequestHighPerformance = 1;
}
#endif

static int run_app(int argc, char** argv) {
    std::string url         = "ws://127.0.0.1:8765";
    bool        spawn_back  = true;
    bool        dev_hud     = false;
    double      reload_interval_s = 0.8;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if ((arg == "--url" || arg == "-u") && i + 1 < argc) {
            url = argv[++i];
        } else if (arg == "--no-backend") {
            spawn_back = false;
        } else if (arg == "--dev-hud") {
            dev_hud = true;
        } else if (arg == "--reload-interval" && i + 1 < argc) {
            try {
                reload_interval_s = std::stod(argv[++i]);
            } catch (...) {
                reload_interval_s = 0.8;
            }
        } else if (arg == "--help" || arg == "-h") {
            std::printf(
                "Usage: jarvis [--url ws://host:port] [--no-backend] "
                "[--dev-hud] [--reload-interval seconds]\n"
            );
            return 0;
        }
    }
    if (const char* env = std::getenv("JARVIS_URL")) {
        url = env;
    }
    if (const char* env = std::getenv("JARVIS_DEV_HUD")) {
        std::string v = env;
        if (v == "1" || v == "true" || v == "TRUE" || v == "yes" || v == "YES") {
            dev_hud = true;
        }
    }

    ix::initNetSystem();

    jarvis::BackendLauncher backend;
    if (spawn_back) {
        std::string info;
        if (backend.start(info, dev_hud, reload_interval_s)) {
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

int main(int argc, char** argv) {
    return run_app(argc, argv);
}

#ifdef _WIN32
int WINAPI WinMain(HINSTANCE, HINSTANCE, LPSTR, int) {
    return run_app(__argc, __argv);
}
#endif

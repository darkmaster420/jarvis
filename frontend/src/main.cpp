#ifdef _WIN32
#  include <winsock2.h>
#  include <ws2tcpip.h>
#endif

#include "BackendLauncher.h"
#include "Hud.h"
#include "State.h"
#include "WsClient.h"

#include <ixwebsocket/IXNetSystem.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <string>
#include <thread>

#ifdef _WIN32

extern "C" {
__declspec(dllexport) DWORD NvOptimusEnablement = 0x00000001;
__declspec(dllexport) int AmdPowerXpressRequestHighPerformance = 1;
}
#endif

static int run_app(int argc, char** argv) {
    std::string url         = "ws://127.0.0.1:8765";
    bool        spawn_back  = true;
#ifdef JARVIS_DEBUG_HUD
    bool        dev_hud     = true;
    const bool  backend_debug_logs = true;
#else
    bool        dev_hud     = false;
    const bool  backend_debug_logs = false;
#endif
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
#ifdef JARVIS_DEBUG_HUD
            std::printf(
                "Usage: jarvis-debug [--url ws://host:port] [--no-backend] "
                "[--dev-hud] [--reload-interval seconds]\n"
                "Debug build: console attached; backend spawned with "
                "--log-level DEBUG; --dev-hud on by default.\n");
#else
            std::printf(
                "Usage: jarvis [--url ws://host:port] [--no-backend] "
                "[--dev-hud] [--reload-interval seconds]\n");
#endif
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
#ifdef JARVIS_DEBUG_HUD
    struct BackendLogTailer {
        std::atomic<bool> running{false};
        std::thread worker;

        void start(const std::string& path) {
            stop();
            if (path.empty()) return;
            running = true;
            worker = std::thread([this, path]() {
                std::ifstream in(path, std::ios::in);
                if (in) {
                    in.seekg(0, std::ios::end);
                }
                while (running) {
                    if (!in.is_open()) {
                        in.clear();
                        in.open(path, std::ios::in);
                        if (in) in.seekg(0, std::ios::end);
                        std::this_thread::sleep_for(std::chrono::milliseconds(180));
                        continue;
                    }
                    std::string line;
                    if (std::getline(in, line)) {
                        std::fprintf(stderr, "[backend] %s\n", line.c_str());
                        continue;
                    }
                    in.clear();
                    std::this_thread::sleep_for(std::chrono::milliseconds(120));
                }
            });
        }

        void stop() {
            running = false;
            if (worker.joinable()) worker.join();
        }
    } backend_log_tailer;
#endif
    if (spawn_back) {
        std::string info;
        if (backend.start(info, dev_hud, reload_interval_s, backend_debug_logs)) {
            std::fprintf(stderr, "[jarvis] %s\n", info.c_str());
        } else {
            std::fprintf(stderr, "[jarvis] backend launch failed: %s\n",
                         info.c_str());
        }
    }
#ifdef JARVIS_DEBUG_HUD
    backend_log_tailer.start(backend.logPath());
#endif

    jarvis::SharedState state;
    jarvis::WsClient    ws(state, url);
    auto restart_backend = [&backend, dev_hud, reload_interval_s,
                            backend_debug_logs]() -> std::string {
        backend.stop();
        std::string info;
        if (backend.start(info, dev_hud, reload_interval_s, backend_debug_logs)) {
            return "Backend restarted. " + info;
        }
        return "Backend restart failed: " + info;
    };
    jarvis::Hud         hud(state, ws, backend.logPath(), restart_backend);

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
#ifdef JARVIS_DEBUG_HUD
    backend_log_tailer.stop();
#endif
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

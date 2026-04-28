#pragma once
#include <filesystem>
#include <string>

#ifdef _WIN32
#  include <windows.h>
#endif

namespace jarvis {

// Spawns the Python backend as a child tied to the HUD's lifetime via a
// Windows Job Object (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE). When this process
// dies - for any reason, including crashes and taskkill /f - the job closes
// and Windows terminates the backend too.
class BackendLauncher {
public:
    BackendLauncher() = default;
    ~BackendLauncher() { stop(); }

    BackendLauncher(const BackendLauncher&)            = delete;
    BackendLauncher& operator=(const BackendLauncher&) = delete;

    // Returns false only on unexpected errors; a "skip" (backend already
    // running, no python interpreter bundled, etc.) is not treated as an
    // error. On skip, spawned() returns false.
    bool start(std::string& info, bool dev_reload = false,
               double reload_interval_s = 0.8);
    void stop();

    bool spawned() const { return spawned_; }

    // Absolute path to backend.log. Populated lazily even when start() is
    // skipped (so the HUD can tail the log of an externally-run backend).
    std::string logPath() const;

private:
    bool spawned_ = false;
    std::filesystem::path log_path_;
#ifdef _WIN32
    HANDLE job_     = nullptr;
    HANDLE process_ = nullptr;

    static bool findBackend(std::filesystem::path& python_exe,
                            std::filesystem::path& repo_root);
    static bool portInUse(unsigned short port);
#endif
};

}  // namespace jarvis

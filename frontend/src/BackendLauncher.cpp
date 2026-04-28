#ifdef _WIN32
// winsock2.h must come before windows.h (pulled in via BackendLauncher.h)
#  include <winsock2.h>
#  include <ws2tcpip.h>
#endif

#include "BackendLauncher.h"

#include <array>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

namespace fs = std::filesystem;

namespace jarvis {

#ifdef _WIN32

static std::string fromWide(const std::wstring& s) {
    if (s.empty()) return "";
    int n = WideCharToMultiByte(CP_UTF8, 0, s.data(), (int)s.size(),
                                nullptr, 0, nullptr, nullptr);
    std::string out(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, s.data(), (int)s.size(),
                        out.data(), n, nullptr, nullptr);
    return out;
}

static fs::path exeDir() {
    wchar_t buf[MAX_PATH * 2] = {};
    DWORD n = GetModuleFileNameW(nullptr, buf, (DWORD)std::size(buf));
    if (n == 0) return fs::current_path();
    return fs::path(buf).parent_path();
}

static void appendLaunchLog(const fs::path& log_path, const std::string& msg) {
    if (log_path.empty()) return;
    try {
        std::ofstream out(log_path, std::ios::app);
        out << "[jarvis launcher] " << msg << "\n";
    } catch (...) {
        // Best effort only; the HUD still shows the status line.
    }
}

bool BackendLauncher::findBackend(fs::path& python_exe, fs::path& repo_root) {
    fs::path start = exeDir();
    for (int i = 0; i < 6; ++i) {
        fs::path candidate = start / "backend" / ".venv" / "Scripts"
                           / "python.exe";
        if (fs::exists(candidate)) {
            python_exe = candidate;
            repo_root  = start;
            return true;
        }
        if (!start.has_parent_path() || start.parent_path() == start) break;
        start = start.parent_path();
    }
    return false;
}

bool BackendLauncher::portInUse(unsigned short port) {
    WSADATA wsa{};
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) return false;
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    bool in_use = false;
    if (s != INVALID_SOCKET) {
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port   = htons(port);
        inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
        if (connect(s, (sockaddr*)&addr, sizeof(addr)) == 0) in_use = true;
        closesocket(s);
    }
    WSACleanup();
    return in_use;
}

bool BackendLauncher::start(std::string& info, bool dev_reload,
                            double reload_interval_s) {
    info.clear();
    fs::path app_root = exeDir();
    log_path_ = app_root / "backend.log";

    // Locate the log path up-front so the HUD can tail it even if we
    // decide not to spawn (port already in use, no venv found, ...).
    {
        fs::path py, repo;
        if (findBackend(py, repo)) {
            log_path_ = repo / "backend.log";
        }
    }

    if (portInUse(8765)) {
        info = "backend already running on 127.0.0.1:8765; attaching";
        return true;  // not an error, just skip spawn
    }

    fs::path python, repo;
    if (!findBackend(python, repo)) {
        fs::path expected = app_root / "backend" / ".venv" / "Scripts"
                          / "python.exe";
        info = "backend runtime missing; expected " + expected.string() +
               ". Reinstall Jarvis or run _install/setup_runtime.ps1.";
        appendLaunchLog(log_path_, info);
        return true;
    }

    fs::path cfg_default = repo / "config.default.yaml";
    fs::path cfg_legacy   = repo / "config.yaml";
    fs::path backend_pkg = repo / "backend" / "jarvis" / "main.py";
    if (!fs::exists(cfg_default) && !fs::exists(cfg_legacy)) {
        info = "neither config.default.yaml nor config.yaml found under " + repo.string();
        appendLaunchLog(log_path_, info);
        return false;
    }
    if (!fs::exists(backend_pkg)) {
        info = "backend package missing at " + backend_pkg.string();
        appendLaunchLog(log_path_, info);
        return false;
    }

    // Job object that kills the backend when the HUD process exits.
    job_ = CreateJobObjectW(nullptr, nullptr);
    if (!job_) {
        info = "CreateJobObject failed";
        return false;
    }
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION jeli{};
    jeli.BasicLimitInformation.LimitFlags =
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE |
        JOB_OBJECT_LIMIT_BREAKAWAY_OK      |
        JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK;
    if (!SetInformationJobObject(job_, JobObjectExtendedLimitInformation,
                                  &jeli, sizeof(jeli))) {
        CloseHandle(job_); job_ = nullptr;
        info = "SetInformationJobObject failed";
        return false;
    }

    // Redirect backend stdout/stderr to a log file so the user can debug.
    fs::path log_path = repo / "backend.log";
    log_path_ = log_path;
    SECURITY_ATTRIBUTES sa{};
    sa.nLength        = sizeof(sa);
    sa.bInheritHandle = TRUE;
    HANDLE log = CreateFileW(
        log_path.wstring().c_str(),
        FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
        &sa, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);

    // Install root holds config.default.yaml; user overrides load from %LocalAppData%\Jarvis.
    std::wstringstream cmd;
    cmd << L"\"" << python.wstring() << L"\" -m jarvis.main"
        << L" --install-root \"" << repo.wstring() << L"\"";
    if (dev_reload) {
        int reload_ms = static_cast<int>(reload_interval_s * 1000.0);
        if (reload_ms < 200) reload_ms = 200;
        reload_interval_s = static_cast<double>(reload_ms) / 1000.0;
        cmd << L" --dev-reload --reload-interval "
            << std::to_wstring(reload_interval_s);
    }
    std::wstring cmdline = cmd.str();

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    if (log != INVALID_HANDLE_VALUE) {
        si.dwFlags   |= STARTF_USESTDHANDLES;
        si.hStdInput  = GetStdHandle(STD_INPUT_HANDLE);
        si.hStdOutput = log;
        si.hStdError  = log;
    }
    PROCESS_INFORMATION pi{};
    DWORD flags = CREATE_NO_WINDOW | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT;
    // Run from backend/ so `jarvis` is discoverable on sys.path.
    std::wstring workdir = (repo / "backend").wstring();

    std::vector<wchar_t> mut(cmdline.begin(), cmdline.end());
    mut.push_back(L'\0');

    BOOL ok = CreateProcessW(
        nullptr, mut.data(), nullptr, nullptr, /*inherit=*/ TRUE,
        flags, nullptr, workdir.c_str(), &si, &pi);
    if (log != INVALID_HANDLE_VALUE) CloseHandle(log);
    if (!ok) {
        DWORD err = GetLastError();
        CloseHandle(job_); job_ = nullptr;
        info = "CreateProcess failed (error " + std::to_string(err) +
               ") using " + python.string();
        appendLaunchLog(log_path_, info);
        return false;
    }

    if (!AssignProcessToJobObject(job_, pi.hProcess)) {
        DWORD err = GetLastError();
        TerminateProcess(pi.hProcess, 1);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        CloseHandle(job_); job_ = nullptr;
        info = "AssignProcessToJobObject failed (error " +
               std::to_string(err) + ")";
        appendLaunchLog(log_path_, info);
        return false;
    }
    ResumeThread(pi.hThread);
    CloseHandle(pi.hThread);
    process_ = pi.hProcess;
    spawned_ = true;
    info = "backend spawned (pid " + std::to_string(pi.dwProcessId) +
           (dev_reload ? ", dev-reload ON" : "") +
           "); logs: " + fromWide(log_path.wstring());
    return true;
}

void BackendLauncher::stop() {
    // Closing the job handle terminates every process in it (the backend).
    if (process_) { CloseHandle(process_); process_ = nullptr; }
    if (job_)     { CloseHandle(job_);     job_     = nullptr; }
    spawned_ = false;
}

#else  // non-Windows stubs

bool BackendLauncher::start(std::string& info) {
    info = "BackendLauncher only implemented on Windows";
    return true;
}
void BackendLauncher::stop() {}

#endif

std::string BackendLauncher::logPath() const {
    return log_path_.empty() ? std::string{} : log_path_.string();
}

}  // namespace jarvis

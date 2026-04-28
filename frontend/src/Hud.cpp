#include "Hud.h"
#include "WsClient.h"

#include <imgui.h>
#include <imgui_impl_win32.h>
#include <imgui_impl_dx11.h>

#include <nlohmann/json.hpp>

#include <d3d11.h>
#include <dwmapi.h>
#include <dxgi.h>

// MinGW / older SDK: 32-bit builds often expose GWL_EXSTYLE only; MSVC maps
// GetWindowLongPtr -> GetWindowLong there, but GWLP_EXSTYLE may be missing.
#ifndef GWLP_EXSTYLE
#  ifdef GWL_EXSTYLE
#    define GWLP_EXSTYLE GWL_EXSTYLE
#  else
#    define GWLP_EXSTYLE (-20)
#  endif
#endif

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdint>
#include <cwctype>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#ifndef JARVIS_HUD_VERSION_STR
#define JARVIS_HUD_VERSION_STR "0.0.0+local"
#endif

extern IMGUI_IMPL_API LRESULT
ImGui_ImplWin32_WndProcHandler(HWND hWnd, UINT msg, WPARAM wParam, LPARAM lParam);

namespace jarvis {

namespace {
namespace fs = std::filesystem;

// Wider/taller than v0.2.x so settings rows (combos + Save/Go) are not squeezed.
constexpr int   kWinW      = 420;
constexpr int   kWinH      = 520;
constexpr int   kTextPanelY = 228;  // y offset for log area under orb
constexpr float kOrbRadius = 72.0f;
constexpr float kPi        = 3.14159265358979323846f;
constexpr int   kBorderDragPx = 10;  // drag by grabbing the window edge (not ImGui)

const wchar_t* kClassName = L"JarvisHudWindowClass";

fs::path exeDir() {
    wchar_t buf[MAX_PATH * 2] = {};
    DWORD n = GetModuleFileNameW(nullptr, buf, (DWORD)std::size(buf));
    if (n == 0) return fs::current_path();
    return fs::path(buf).parent_path();
}

/** Under Program Files, writable HUD state lives in %LocalAppData%\\Jarvis. */
fs::path jarvisUserDataDir() {
    fs::path ed = exeDir();
    std::wstring w = ed.wstring();
    for (wchar_t& c : w) {
        c = (wchar_t)std::towlower((wint_t)(unsigned short)c);
    }
    if (w.find(L"\\program files\\") != std::wstring::npos ||
        w.find(L"\\program files (x86)\\") != std::wstring::npos) {
        wchar_t la[MAX_PATH * 2] = {};
        DWORD n = GetEnvironmentVariableW(L"LOCALAPPDATA", la, (DWORD)std::size(la));
        if (n > 0 && n < std::size(la)) {
            fs::path p = fs::path(la) / L"Jarvis";
            try {
                fs::create_directories(p);
            } catch (...) {
            }
            return p;
        }
    }
    return ed;
}

/** Match backend ``user_data_dir``: AppData for installed builds, else next to the exe. */
fs::path hudLayoutPath() {
    fs::path primary = jarvisUserDataDir() / L"hud_layout.json";
    fs::path legacy  = exeDir() / L"hud_layout.json";
    if (primary != legacy && !fs::exists(primary) && fs::exists(legacy)) {
        try {
            fs::create_directories(primary.parent_path());
            fs::copy_file(legacy, primary, fs::copy_options::skip_existing);
        } catch (...) {
        }
    }
    return primary;
}

void clampToVisible(RECT* r) {
    if (!r) return;
    HMONITOR mon = MonitorFromPoint(
        POINT{r->left, r->top}, MONITOR_DEFAULTTONEAREST);
    if (!mon) return;
    MONITORINFO mi{};
    mi.cbSize = sizeof(mi);
    if (!GetMonitorInfoW(mon, &mi)) return;
    int w = r->right - r->left;
    int h = r->bottom - r->top;
    if (w < 1 || h < 1) return;
    if (r->left   < mi.rcWork.left)   { r->left = mi.rcWork.left;   r->right  = r->left + w; }
    if (r->top    < mi.rcWork.top)    { r->top  = mi.rcWork.top;    r->bottom = r->top + h;  }
    if (r->right  > mi.rcWork.right)  { r->right  = mi.rcWork.right;  r->left  = r->right - w;  }
    if (r->bottom > mi.rcWork.bottom) { r->bottom = mi.rcWork.bottom; r->top  = r->bottom - h; }
}

void loadWindowLayout(HWND hwnd, const fs::path& file) {
    if (!hwnd) return;
    try {
        if (!fs::exists(file)) return;
        std::ifstream in(file, std::ios::in | std::ios::binary);
        nlohmann::json j;
        in >> j;
        int x = j.value("x", INT_MIN);
        int y = j.value("y", INT_MIN);
        if (x == INT_MIN || y == INT_MIN) return;
        RECT r{ x, y, x + kWinW, y + kWinH };
        clampToVisible(&r);
        SetWindowPos(
            hwnd, nullptr, r.left, r.top, kWinW, kWinH,
            SWP_NOZORDER | SWP_NOACTIVATE);
    } catch (...) {
    }
}

void saveWindowLayout(HWND hwnd, const fs::path& file) {
    if (!hwnd) return;
    try {
        RECT r{};
        if (!GetWindowRect(hwnd, &r)) return;
        nlohmann::json j;
        j["x"] = r.left;
        j["y"] = r.top;
        j["w"] = r.right - r.left;
        j["h"] = r.bottom - r.top;
        std::ofstream out(file, std::ios::out | std::ios::binary | std::ios::trunc);
        out << j.dump(2);
    } catch (...) {
    }
}

LRESULT nchitTestBorderDrag(HWND hwnd, LPARAM lp) {
    if (!hwnd) return DefWindowProcW(hwnd, WM_NCHITTEST, 0, lp);
    POINT pt{ (LONG)(short)LOWORD(lp), (LONG)(short)HIWORD(lp) };
    ScreenToClient(hwnd, &pt);
    RECT cr{};
    if (!GetClientRect(hwnd, &cr)) return DefWindowProcW(hwnd, WM_NCHITTEST, 0, lp);
    int w = (int)(cr.right - cr.left);
    int h = (int)(cr.bottom - cr.top);
    if (w <= 0 || h <= 0) return DefWindowProcW(hwnd, WM_NCHITTEST, 0, lp);
    const int b = kBorderDragPx;
    if (pt.x < b || pt.x >= w - b || pt.y < b || pt.y >= h - b) {
        return HTCAPTION;
    }
    return DefWindowProcW(hwnd, WM_NCHITTEST, 0, lp);
}

struct OrbColor { float r, g, b; };

// Main HUD uses a transparent WindowBg so the gradient shows. Full-screen
// overlays must paint opaquely or the speech log from ##jarvis bleeds through.
static const ImVec4 kOverlayWindowBg(0.02f, 0.04f, 0.10f, 0.99f);
static const ImU32  kCyan  = IM_COL32(0, 255, 220, 255);
static const ImU32  kCyan2 = IM_COL32(0, 200, 255, 100);
static const ImU32  kMag   = IM_COL32(255, 0, 200, 80);

static size_t utf8_count_cp(const std::string& s) {
    size_t n = 0, i = 0;
    while (i < s.size()) {
        unsigned char c = (unsigned char)s[i];
        size_t w = 1u;
        if (c < 0x80) w = 1u;
        else if ((c & 0xf0) == 0xf0) w = 4u;
        else if ((c & 0xe0) == 0xe0) w = 3u;
        else if ((c & 0xc0) == 0xc0) w = 2u;
        if (i + w > s.size()) break;
        i += w; ++n;
    }
    return n;
}

static std::string utf8_prefix_cp(const std::string& s, size_t n_cp) {
    if (n_cp == 0) return {};
    size_t n = 0, i = 0;
    while (i < s.size() && n < n_cp) {
        unsigned char c = (unsigned char)s[i];
        size_t w = 1u;
        if (c < 0x80) w = 1u;
        else if ((c & 0xf0) == 0xf0) w = 4u;
        else if ((c & 0xe0) == 0xe0) w = 3u;
        else if ((c & 0xc0) == 0xc0) w = 2u;
        if (i + w > s.size()) break;
        i += w; ++n;
    }
    return s.substr(0, i);
}

/** Corner brackets, tick marks, horizontal scan line — under widgets. */
static void drawCyberpunkScaffold(ImDrawList* dl, const ImVec2& p0, const ImVec2& p1,
                                 float r, double now_sec) {
    const float L = 10.0f;
    // Outer neon edge
    dl->AddRect(p0, p1, kCyan2, r, 0, 1.0f);
    // Corner L-brackets
    const ImU32 c = kCyan;
    dl->AddLine(ImVec2(p0.x + 2, p0.y + 2 + L), ImVec2(p0.x + 2, p0.y + 2), c, 1.4f);
    dl->AddLine(ImVec2(p0.x + 2, p0.y + 2), ImVec2(p0.x + 2 + L, p0.y + 2), c, 1.4f);
    dl->AddLine(ImVec2(p1.x - 2, p0.y + 2), ImVec2(p1.x - 2, p0.y + 2 + L), c, 1.4f);
    dl->AddLine(ImVec2(p1.x - 2, p0.y + 2), ImVec2(p1.x - 2 - L, p0.y + 2), c, 1.4f);
    dl->AddLine(ImVec2(p0.x + 2, p1.y - 2 - L), ImVec2(p0.x + 2, p1.y - 2), c, 1.4f);
    dl->AddLine(ImVec2(p0.x + 2, p1.y - 2), ImVec2(p0.x + 2 + L, p1.y - 2), c, 1.4f);
    dl->AddLine(ImVec2(p1.x - 2, p1.y - 2), ImVec2(p1.x - 2, p1.y - 2 - L), c, 1.4f);
    dl->AddLine(ImVec2(p1.x - 2, p1.y - 2), ImVec2(p1.x - 2 - L, p1.y - 2), c, 1.4f);
    // Data ticks along top
    for (int i = 1; i < 8; ++i) {
        float x = p0.x + 24.0f + i * 38.0f;
        if (x >= p1.x - 30.0f) break;
        float h = (i & 1) ? 3.0f : 2.0f;
        dl->AddLine(ImVec2(x, p0.y + 3.0f), ImVec2(x, p0.y + 3.0f + h), c, 1.0f);
    }
    // Slow horizontal scan
    const float s = 0.5f + 0.5f * std::sin(float(now_sec) * 0.6f);
    const float y = p0.y + 8.0f + s * (p1.y - p0.y - 16.0f);
    dl->AddLine(
        ImVec2(p0.x + 6.0f, y), ImVec2(p1.x - 6.0f, y), IM_COL32(0, 255, 200, 25), 1.0f);
    dl->AddLine(ImVec2(p1.x - 18.0f, p1.y - 6.0f), ImVec2(p1.x - 4.0f, p1.y - 6.0f), kMag, 1.2f);
}

bool isIntegratedAdapter(const DXGI_ADAPTER_DESC1& desc) {
    // Intel is the common integrated GPU on Windows laptops/desktops. Prefer
    // NVIDIA/AMD/dGPU adapters when present, then fall back to any hardware.
    return desc.VendorId == 0x8086;
}

IDXGIAdapter1* chooseHighPerformanceAdapter() {
    IDXGIFactory1* factory = nullptr;
    if (FAILED(CreateDXGIFactory1(__uuidof(IDXGIFactory1),
                                  reinterpret_cast<void**>(&factory)))) {
        return nullptr;
    }

    IDXGIAdapter1* best_discrete = nullptr;
    IDXGIAdapter1* best_hardware = nullptr;
    SIZE_T best_discrete_mem = 0;
    SIZE_T best_hardware_mem = 0;

    for (UINT i = 0;; ++i) {
        IDXGIAdapter1* adapter = nullptr;
        if (factory->EnumAdapters1(i, &adapter) == DXGI_ERROR_NOT_FOUND) {
            break;
        }
        DXGI_ADAPTER_DESC1 desc{};
        if (SUCCEEDED(adapter->GetDesc1(&desc)) &&
            !(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)) {
            if (!isIntegratedAdapter(desc) &&
                desc.DedicatedVideoMemory >= best_discrete_mem) {
                if (best_discrete) best_discrete->Release();
                best_discrete = adapter;
                best_discrete_mem = desc.DedicatedVideoMemory;
                adapter = nullptr;
            } else if (desc.DedicatedVideoMemory >= best_hardware_mem) {
                if (best_hardware) best_hardware->Release();
                best_hardware = adapter;
                best_hardware_mem = desc.DedicatedVideoMemory;
                adapter = nullptr;
            }
        }
        if (adapter) adapter->Release();
    }

    factory->Release();
    if (best_discrete) {
        if (best_hardware) best_hardware->Release();
        return best_discrete;
    }
    return best_hardware;
}

OrbColor colorFor(HudState s) {
    // Holographic / HUD palette (slate base + neon accents)
    switch (s) {
        case HudState::Disconnected: return {0.32f, 0.36f, 0.45f};
        case HudState::Idle:         return {0.15f, 0.88f, 0.98f};
        case HudState::Listening:    return {0.45f, 0.60f, 1.00f};
        case HudState::Thinking:     return {1.00f, 0.55f, 0.20f};
        case HudState::Speaking:     return {0.20f, 0.95f, 0.70f};
    }
    return {1.0f, 1.0f, 1.0f};
}

ImU32 toU32(OrbColor c, float a) {
    auto clamp = [](float x) { return std::max(0.0f, std::min(1.0f, x)); };
    return IM_COL32(int(clamp(c.r) * 255), int(clamp(c.g) * 255),
                    int(clamp(c.b) * 255), int(clamp(a) * 255));
}

LRESULT CALLBACK WndProcThunk(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    Hud* self = reinterpret_cast<Hud*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    if (msg == WM_NCCREATE) {
        CREATESTRUCTW* cs = reinterpret_cast<CREATESTRUCTW*>(lp);
        self = reinterpret_cast<Hud*>(cs->lpCreateParams);
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(self));
    }
    if (self) {
        return self->handleMessage(hwnd, msg, wp, lp);
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}
} // namespace

Hud::Hud(SharedState& state, WsClient& ws, std::string log_path,
         std::function<std::string()> restart_backend)
    : state_(state),
      ws_(ws),
      restart_backend_(std::move(restart_backend)),
      log_path_(std::move(log_path)) {}
Hud::~Hud() { shutdown(); }

LRESULT Hud::handleMessage(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    if (msg == WM_NCHITTEST) {
        return nchitTestBorderDrag(hwnd, lp);
    }
    const bool handled_imgui = ImGui_ImplWin32_WndProcHandler(
        hwnd, msg, wp, lp) != 0;
    if (handled_imgui) {
        // ImGui consumes mouse down before Windows would activate the window.
        // Without raising + focusing, WS_EX_TOPMOST popups never get keyboard
        // input, so text fields, Ctrl+V, and IME do nothing.
        if (msg == WM_LBUTTONDOWN || msg == WM_LBUTTONDBLCLK) {
            ImGuiIO& io = ImGui::GetIO();
            if (io.WantCaptureMouse) {
                if (GetForegroundWindow() != hwnd) {
                    SetForegroundWindow(hwnd);
                }
                if (GetFocus() != hwnd) {
                    SetFocus(hwnd);
                }
            }
        }
        return 0;
    }
    switch (msg) {
        case WM_SIZE:
            if (wp == SIZE_RESTORED) {
                // Use `hwnd` — WM_SIZE can arrive before `hwnd_` is assigned in init().
                restoreOverlayWindowStyles(hwnd);
            }
            if (wp != SIZE_MINIMIZED && device_) {
                resize_w_ = (UINT)LOWORD(lp);
                resize_h_ = (UINT)HIWORD(lp);
            }
            return 0;
        case WM_SYSCOMMAND:
            if ((wp & 0xfff0) == SC_KEYMENU) return 0;
            break;
        case WM_CLOSE:
        case WM_DESTROY:
            running_ = false;
            PostQuitMessage(0);
            return 0;
        case WM_LBUTTONDOWN: {
            ImGuiIO& io = ImGui::GetIO();
            if (!io.WantCaptureMouse) {
                ReleaseCapture();
                SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, 0);
                return 0;
            }
            break;
        }
        default:
            break;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

void Hud::minimizeToTaskbar() {
    if (!hwnd_) return;
    // WS_EX_TOOLWINDOW hides the window from the taskbar; swap to APPWINDOW
    // so minimize lands on the taskbar like a normal app.
    auto ex = static_cast<ULONG_PTR>(GetWindowLongPtr(hwnd_, GWLP_EXSTYLE));
    ex |= static_cast<ULONG_PTR>(WS_EX_APPWINDOW);
    ex &= ~static_cast<ULONG_PTR>(WS_EX_TOOLWINDOW);
    SetWindowLongPtr(hwnd_, GWLP_EXSTYLE, static_cast<LONG_PTR>(ex));
    SetWindowPos(
        hwnd_, HWND_NOTOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
    ShowWindow(hwnd_, SW_MINIMIZE);
}

void Hud::restoreOverlayWindowStyles(HWND hwnd) {
    if (!hwnd) return;
    auto ex = static_cast<ULONG_PTR>(GetWindowLongPtr(hwnd, GWLP_EXSTYLE));
    ex &= ~static_cast<ULONG_PTR>(WS_EX_APPWINDOW);
    ex |= static_cast<ULONG_PTR>(WS_EX_TOPMOST | WS_EX_TOOLWINDOW);
    SetWindowLongPtr(hwnd, GWLP_EXSTYLE, static_cast<LONG_PTR>(ex));
    SetWindowPos(
        hwnd, HWND_TOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE);
}

bool Hud::init() {
    hinst_ = GetModuleHandleW(nullptr);

    WNDCLASSEXW wc{};
    wc.cbSize        = sizeof(wc);
    wc.style         = CS_CLASSDC;
    wc.lpfnWndProc   = WndProcThunk;
    wc.hInstance     = hinst_;
    wc.hCursor       = LoadCursor(nullptr, IDC_ARROW);
    wc.lpszClassName = kClassName;
    if (!RegisterClassExW(&wc)) {
        std::fprintf(stderr, "RegisterClassExW failed: %lu\n", GetLastError());
        return false;
    }

    int screenW = GetSystemMetrics(SM_CXSCREEN);
    int posX    = screenW - kWinW - 24;
    int posY    = 48;

    DWORD style   = WS_POPUP;
    // WS_EX_NOACTIVATE prevents the window from ever taking focus, so typing
    // and paste go to whatever app was previously in the foreground.
    DWORD exStyle = WS_EX_TOPMOST | WS_EX_TOOLWINDOW;

    hwnd_ = CreateWindowExW(
        exStyle, kClassName, L"Jarvis", style,
        posX, posY, kWinW, kWinH,
        nullptr, nullptr, hinst_, this);
    if (!hwnd_) {
        std::fprintf(stderr, "CreateWindowExW failed: %lu\n", GetLastError());
        return false;
    }

    layout_path_ = hudLayoutPath().string();
    loadWindowLayout(hwnd_, fs::path(layout_path_));

    BOOL dark = TRUE;
    DwmSetWindowAttribute(hwnd_, 20 /*DWMWA_USE_IMMERSIVE_DARK_MODE*/, &dark, sizeof(dark));

    if (!createDeviceD3D(hwnd_)) {
        std::fprintf(stderr, "createDeviceD3D failed\n");
        DestroyWindow(hwnd_);
        UnregisterClassW(kClassName, hinst_);
        return false;
    }

    ShowWindow(hwnd_, SW_SHOWNA);
    UpdateWindow(hwnd_);

    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.IniFilename = nullptr;
    io.ConfigFlags |= ImGuiConfigFlags_NoMouseCursorChange;

    ImGui::StyleColorsDark();
    ImGuiStyle& s = ImGui::GetStyle();
    s.WindowRounding   = 16.0f;
    s.ChildRounding    = 10.0f;
    s.FrameRounding    = 6.0f;
    s.WindowBorderSize = 0.0f;
    s.WindowPadding    = ImVec2(14, 12);
    s.ItemSpacing      = ImVec2(10, 9);
    s.ItemInnerSpacing = ImVec2(10, 6);
    s.FramePadding     = ImVec2(12, 8);
    s.GrabMinSize      = 14.0f;
    s.SeparatorTextBorderSize = 1.0f;
    s.SeparatorTextPadding    = ImVec2(12, 6);
    s.TabRounding      = 4.0f;
    s.Colors[ImGuiCol_WindowBg]  = ImVec4(0.01f, 0.03f, 0.07f, 0.25f);
    s.Colors[ImGuiCol_ChildBg]   = ImVec4(0.02f, 0.05f, 0.10f, 0.90f);
    s.Colors[ImGuiCol_Border]    = ImVec4(0.0f, 0.95f, 0.85f, 0.30f);
    s.Colors[ImGuiCol_Text]      = ImVec4(0.88f, 0.94f, 0.98f, 0.98f);
    s.Colors[ImGuiCol_TextDisabled] = ImVec4(0.40f, 0.48f, 0.52f, 0.85f);
    s.Colors[ImGuiCol_Separator] = ImVec4(0.0f, 0.70f, 0.80f, 0.40f);
    s.Colors[ImGuiCol_Header]    = ImVec4(0.08f, 0.25f, 0.30f, 0.80f);
    s.Colors[ImGuiCol_HeaderHovered]  = ImVec4(0.10f, 0.40f, 0.50f, 0.90f);
    s.Colors[ImGuiCol_HeaderActive]  = ImVec4(0.0f, 0.50f, 0.45f, 0.95f);
    s.Colors[ImGuiCol_CheckMark] = ImVec4(0.2f, 1.0f, 0.9f, 1.0f);
    s.Colors[ImGuiCol_SliderGrab]     = ImVec4(0.0f, 0.9f, 0.8f, 0.9f);
    s.Colors[ImGuiCol_SliderGrabActive] = ImVec4(0.2f, 1.0f, 0.95f, 1.0f);
    s.Colors[ImGuiCol_ScrollbarBg]        = ImVec4(0.05f, 0.08f, 0.12f, 0.70f);
    s.Colors[ImGuiCol_ScrollbarGrab]      = ImVec4(0.20f, 0.60f, 0.75f, 0.50f);
    s.Colors[ImGuiCol_ScrollbarGrabHovered] = ImVec4(0.30f, 0.80f, 0.90f, 0.80f);
    s.Colors[ImGuiCol_Button] = ImVec4(0.08f, 0.28f, 0.32f, 0.95f);
    s.Colors[ImGuiCol_ButtonHovered] = ImVec4(0.1f, 0.45f, 0.5f, 1.0f);
    s.Colors[ImGuiCol_ButtonActive]  = ImVec4(0.0f, 0.5f, 0.45f, 1.0f);
    s.Colors[ImGuiCol_FrameBg]  = ImVec4(0.04f, 0.1f, 0.12f, 0.9f);
    s.Colors[ImGuiCol_FrameBgHovered]  = ImVec4(0.06f, 0.2f, 0.24f, 0.95f);
    s.Colors[ImGuiCol_FrameBgActive]   = ImVec4(0.08f, 0.28f, 0.3f, 0.95f);
    s.Colors[ImGuiCol_PopupBg]  = kOverlayWindowBg;

    {
        ImGuiIO& fio = ImGui::GetIO();
        // Prefer a crisp monospace; fall back to system UI.
        const char* font_paths[] = {
            "C:/Windows/Fonts/Consolab.ttf",
            "C:/Windows/Fonts/consolab.ttf",
            "C:/Windows/Fonts/Consola.ttf",
            "C:/Windows/Fonts/consola.ttf",
            "C:/Windows/Fonts/SegoeUI.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
        };
        ImFont* def = nullptr;
        for (const char* p : font_paths) {
            def = fio.Fonts->AddFontFromFileTTF(
                p, 16.0f, nullptr, fio.Fonts->GetGlyphRangesDefault());
            if (def) break;
        }
        if (def) fio.FontDefault = def;
    }

    ImGui_ImplWin32_Init(hwnd_);
    ImGui_ImplDX11_Init(device_, context_);
    ImGui_ImplDX11_InvalidateDeviceObjects();
    ImGui_ImplDX11_CreateDeviceObjects();

    start_time_ = std::chrono::duration<double>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
    return true;
}

bool Hud::createDeviceD3D(HWND hwnd) {
    DXGI_SWAP_CHAIN_DESC sd{};
    sd.BufferCount = 2;
    sd.BufferDesc.Width  = 0;
    sd.BufferDesc.Height = 0;
    sd.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    sd.BufferDesc.RefreshRate.Numerator   = 60;
    sd.BufferDesc.RefreshRate.Denominator = 1;
    sd.Flags = DXGI_SWAP_CHAIN_FLAG_ALLOW_MODE_SWITCH;
    sd.BufferUsage   = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    sd.OutputWindow  = hwnd;
    sd.SampleDesc.Count   = 1;
    sd.SampleDesc.Quality = 0;
    sd.Windowed     = TRUE;
    sd.SwapEffect   = DXGI_SWAP_EFFECT_DISCARD;

    UINT flags = 0;
    D3D_FEATURE_LEVEL fl;
    const D3D_FEATURE_LEVEL levels[] = {
        D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_0,
    };
    IDXGIAdapter1* adapter = chooseHighPerformanceAdapter();
    HRESULT hr = D3D11CreateDeviceAndSwapChain(
        adapter, adapter ? D3D_DRIVER_TYPE_UNKNOWN : D3D_DRIVER_TYPE_HARDWARE,
        nullptr, flags,
        levels, (UINT)(sizeof(levels) / sizeof(levels[0])),
        D3D11_SDK_VERSION, &sd,
        &swapchain_, &device_, &fl, &context_);
    if (adapter) adapter->Release();
    if (hr == DXGI_ERROR_UNSUPPORTED) {
        hr = D3D11CreateDeviceAndSwapChain(
            nullptr, D3D_DRIVER_TYPE_WARP, nullptr, flags,
            levels, (UINT)(sizeof(levels) / sizeof(levels[0])),
            D3D11_SDK_VERSION, &sd,
            &swapchain_, &device_, &fl, &context_);
    }
    if (FAILED(hr)) {
        std::fprintf(stderr, "D3D11CreateDeviceAndSwapChain failed: 0x%08lx\n", (unsigned long)hr);
        return false;
    }
    createRenderTarget();
    return true;
}

void Hud::cleanupDeviceD3D() {
    cleanupRenderTarget();
    if (swapchain_) { swapchain_->Release(); swapchain_ = nullptr; }
    if (context_)   { context_->Release();   context_   = nullptr; }
    if (device_)    { device_->Release();    device_    = nullptr; }
}

void Hud::createRenderTarget() {
    ID3D11Texture2D* back = nullptr;
    swapchain_->GetBuffer(0, IID_PPV_ARGS(&back));
    if (back) {
        device_->CreateRenderTargetView(back, nullptr, &rtv_);
        back->Release();
    }
}

void Hud::cleanupRenderTarget() {
    if (rtv_) { rtv_->Release(); rtv_ = nullptr; }
}

void Hud::run() {
    MSG msg{};
    while (running_) {
        while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
            if (msg.message == WM_QUIT) {
                running_ = false;
            }
        }
        if (!running_) break;

        if (resize_w_ != 0 && resize_h_ != 0) {
            cleanupRenderTarget();
            swapchain_->ResizeBuffers(0, resize_w_, resize_h_, DXGI_FORMAT_UNKNOWN, 0);
            resize_w_ = resize_h_ = 0;
            createRenderTarget();
        }

        bool ctrl  = (GetAsyncKeyState(VK_CONTROL) & 0x8000) != 0;
        bool space = (GetAsyncKeyState(VK_SPACE)   & 0x8000) != 0;
        bool down  = ctrl && space;
        if (down && !push_to_talk_down_) {
            ws_.sendCommand("push_to_talk");
        }
        push_to_talk_down_ = down;

        bool f2 = (GetAsyncKeyState(VK_F2) & 0x8000) != 0;
        if (f2 && !f2_down_) {
            show_settings_ = !show_settings_;
            if (show_settings_) {
                ws_.requestSettings();
                ws_.requestUserSkills();
            }
        }
        f2_down_ = f2;

        bool f3 = (GetAsyncKeyState(VK_F3) & 0x8000) != 0;
        if (f3 && !f3_down_) {
            show_patches_ = !show_patches_;
            if (show_patches_) ws_.requestPatches();
        }
        f3_down_ = f3;

        bool f1 = (GetAsyncKeyState(VK_F1) & 0x8000) != 0;
        if (f1 && !f1_down_) {
            show_logs_ = !show_logs_;
            if (show_logs_) refreshLogs();
        }
        f1_down_ = f1;

        // Ctrl+Q quits from anywhere. Also the HUD has an X button.
        bool q = (GetAsyncKeyState('Q') & 0x8000) != 0;
        if (ctrl && q) {
            PostMessageW(hwnd_, WM_CLOSE, 0, 0);
        }

        render();
    }
}

void Hud::render() {
    ImGui_ImplDX11_NewFrame();
    ImGui_ImplWin32_NewFrame();
    ImGui::NewFrame();

    ImGui::SetNextWindowPos(ImVec2(0, 0));
    ImGui::SetNextWindowSize(ImVec2((float)kWinW, (float)kWinH));
    ImGuiWindowFlags flags = ImGuiWindowFlags_NoTitleBar
                           | ImGuiWindowFlags_NoResize
                           | ImGuiWindowFlags_NoMove
                           | ImGuiWindowFlags_NoCollapse
                           | ImGuiWindowFlags_NoScrollbar
                           | ImGuiWindowFlags_NoSavedSettings;
    ImGui::Begin("##jarvis", nullptr, flags);

    {
        // Deep-space gradient + glass frame (drawn under widgets)
        ImDrawList* dl  = ImGui::GetWindowDrawList();
        const ImVec2 p0 = ImGui::GetWindowPos();
        const ImVec2 p1 = ImVec2(p0.x + (float)kWinW, p0.y + (float)kWinH);
        ImU32 c0 = IM_COL32(4, 10, 22, 255);
        ImU32 c1 = IM_COL32(10, 6, 28, 255);
        ImU32 c2 = IM_COL32(6, 20, 36, 255);
        ImU32 c3 = IM_COL32(3, 14, 26, 255);
        dl->AddRectFilledMultiColor(p0, p1, c0, c1, c2, c3);
        const float r = ImGui::GetStyle().WindowRounding;
        dl->AddRect(
            ImVec2(p0.x + 1.0f, p0.y + 1.0f),
            ImVec2(p1.x - 1.0f, p1.y - 1.0f),
            IM_COL32(50, 220, 255, 90), r, 0, 1.2f
        );
        dl->AddRect(
            p0, p1,
            IM_COL32(120, 200, 255, 40), r, 0, 0.8f
        );
        const double tui = std::chrono::duration<double>(
            std::chrono::steady_clock::now().time_since_epoch()).count()
            - start_time_;
        drawCyberpunkScaffold(dl, p0, p1, r, tui);
    }

    const bool overlay =
        show_settings_ || show_patches_ || show_logs_;
    if (!overlay) {
        drawOrb();
        drawTextPanel();
    }

    // Close (X) and minimize (_) — top-left. Minimize sends the HUD to the taskbar.
    {
        const float size = 24.0f;
        const float pad  = 8.0f;
        const float gap  = 6.0f;
        ImDrawList* dl = ImGui::GetWindowDrawList();

        ImVec2 close_pos(pad, pad);
        ImGui::SetCursorPos(close_pos);
        ImGui::InvisibleButton("##close", ImVec2(size, size));
        bool close_hov = ImGui::IsItemHovered();
        if (ImGui::IsItemClicked()) {
            PostMessageW(hwnd_, WM_CLOSE, 0, 0);
        }
        {
            ImVec2 p = ImGui::GetItemRectMin();
            ImU32 col = close_hov
                ? IM_COL32(255, 120, 120, 255)
                : IM_COL32(180, 80,  80,  200);
            float inset = 5.0f;
            ImVec2 a(p.x + inset, p.y + inset);
            ImVec2 b(p.x + size - inset, p.y + size - inset);
            ImVec2 c1(p.x + size - inset, p.y + inset);
            ImVec2 c2(p.x + inset, p.y + size - inset);
            dl->AddLine(a, b, col, 2.0f);
            dl->AddLine(c1, c2, col, 2.0f);
        }

        ImVec2 min_pos(pad + size + gap, pad);
        ImGui::SetCursorPos(min_pos);
        ImGui::InvisibleButton("##minimize", ImVec2(size, size));
        bool min_hov = ImGui::IsItemHovered();
        if (ImGui::IsItemClicked()) {
            minimizeToTaskbar();
        }
        {
            ImVec2 p = ImGui::GetItemRectMin();
            ImU32 col = min_hov
                ? IM_COL32(160, 210, 255, 255)
                : IM_COL32(130, 170, 210, 200);
            float inset = 5.0f;
            float y = p.y + size - inset - 3.0f;
            dl->AddLine(
                ImVec2(p.x + inset, y),
                ImVec2(p.x + size - inset, y),
                col, 2.0f);
        }
    }

    {
        const float size = 28.0f;
        const float pad  = 10.0f;
        ImVec2 btn_pos(kWinW - size - pad, pad);
        ImGui::SetCursorPos(btn_pos);
        ImGui::InvisibleButton("##gear", ImVec2(size, size));
        bool hovered = ImGui::IsItemHovered();
        bool active  = ImGui::IsItemActive();
        if (ImGui::IsItemClicked()) {
            show_settings_ = !show_settings_;
            if (show_settings_) ws_.requestSettings();
        }
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ImVec2 p = ImGui::GetItemRectMin();
        ImVec2 c(p.x + size * 0.5f, p.y + size * 0.5f);
        ImU32 col = show_settings_
            ? IM_COL32(100, 220, 160, 255)
            : (active ? IM_COL32(180, 220, 255, 255)
                      : hovered ? IM_COL32(200, 230, 255, 220)
                                : IM_COL32(150, 180, 210, 180));
        const float r_out  = size * 0.42f;
        const float r_in   = size * 0.28f;
        const float r_hub  = size * 0.12f;
        const int   teeth  = 8;
        for (int i = 0; i < teeth; ++i) {
            float a0 = (kPi * 2.0f) * (i / (float)teeth);
            float a1 = a0 + (kPi * 2.0f) / (teeth * 2);
            ImVec2 q[4] = {
                { c.x + std::cos(a0) * r_in,  c.y + std::sin(a0) * r_in  },
                { c.x + std::cos(a0) * r_out, c.y + std::sin(a0) * r_out },
                { c.x + std::cos(a1) * r_out, c.y + std::sin(a1) * r_out },
                { c.x + std::cos(a1) * r_in,  c.y + std::sin(a1) * r_in  },
            };
            dl->AddConvexPolyFilled(q, 4, col);
        }
        dl->AddCircleFilled(c, r_in, col, 24);
        dl->AddCircleFilled(c, r_hub, IM_COL32(15, 20, 30, 255), 16);
    }

    // Patches / self-edit review: always visible (left of gear). Amber + badge
    // when there are pending proposals; muted when empty. Same as F3.
    size_t pending = 0;
    {
        std::lock_guard<std::mutex> lk(state_.patches_mutex);
        pending = state_.pending_patches.size();
    }
    {
        const float size = 28.0f;
        const float pad  = 10.0f;
        ImVec2 btn_pos(kWinW - size * 2 - pad * 2, pad);
        ImGui::SetCursorPos(btn_pos);
        ImGui::InvisibleButton("##patches", ImVec2(size, size));
        bool hovered = ImGui::IsItemHovered();
        if (ImGui::IsItemClicked()) {
            show_patches_ = !show_patches_;
            if (show_patches_) ws_.requestPatches();
        }
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ImVec2 p = ImGui::GetItemRectMin();
        ImVec2 a(p.x + 4, p.y + 3);
        ImVec2 b(p.x + size - 4, p.y + size - 3);
        if (pending > 0) {
            ImU32 amber = hovered ? IM_COL32(255, 200, 100, 255)
                                  : IM_COL32(230, 170, 70,  220);
            dl->AddRectFilled(a, b, amber, 3.0f);
            dl->AddRect(a, b, IM_COL32(30, 20, 10, 255), 3.0f, 0, 1.5f);
            char buf[8];
            std::snprintf(buf, sizeof(buf), "%zu", pending);
            ImVec2 ts = ImGui::CalcTextSize(buf);
            ImVec2 bc(b.x + 2, p.y - 2);
            float br = std::max(9.0f, ts.x * 0.5f + 4.0f);
            dl->AddCircleFilled(bc, br, IM_COL32(230, 60, 60, 255), 12);
            dl->AddText(ImVec2(bc.x - ts.x * 0.5f, bc.y - ts.y * 0.5f),
                        IM_COL32(255, 255, 255, 255), buf);
        } else {
            ImU32 doc = show_patches_
                ? IM_COL32(100, 220, 160, 255)
                : (hovered ? IM_COL32(200, 230, 255, 200)
                            : IM_COL32(120, 150, 190, 140));
            dl->AddRectFilled(a, b, doc, 3.0f);
            dl->AddRect(a, b, IM_COL32(40, 50, 70, 180), 3.0f, 0, 1.2f);
            // subtle "diff" hint: two short lines inside
            float mx = (a.x + b.x) * 0.5f;
            dl->AddLine(ImVec2(mx - 6, a.y + 7), ImVec2(mx + 6, a.y + 7),
                        IM_COL32(20, 30, 45, 200), 1.2f);
            dl->AddLine(ImVec2(mx - 4, a.y + 11), ImVec2(mx + 8, a.y + 11),
                        IM_COL32(20, 30, 45, 160), 1.2f);
        }
    }

    // Log toggle icon: stack of horizontal lines, sits at kWinW - size*3 - pad*3.
    {
        const float size = 28.0f;
        const float pad  = 10.0f;
        ImVec2 btn_pos(kWinW - size * 3 - pad * 3, pad);
        ImGui::SetCursorPos(btn_pos);
        ImGui::InvisibleButton("##logs", ImVec2(size, size));
        bool hovered = ImGui::IsItemHovered();
        if (ImGui::IsItemClicked()) {
            show_logs_ = !show_logs_;
            if (show_logs_) refreshLogs();
        }
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ImVec2 p = ImGui::GetItemRectMin();
        ImU32 col = show_logs_
            ? IM_COL32(100, 220, 160, 255)
            : (hovered ? IM_COL32(200, 230, 255, 220)
                       : IM_COL32(150, 180, 210, 180));
        for (int i = 0; i < 4; ++i) {
            float y = p.y + 5.0f + i * 3.5f;
            float w = (i == 0 || i == 2) ? size - 8.0f : size - 12.0f;
            dl->AddLine(ImVec2(p.x + 4, y), ImVec2(p.x + 4 + w, y), col, 1.8f);
        }
    }

    drawVersionCorner();
    ImGui::End();

    if (show_settings_) drawSettings();
    if (show_patches_)  drawPatches();
    if (show_logs_)     drawLogs();

    ImGui::Render();

    const float clear[4] = {0.02f, 0.04f, 0.09f, 1.0f};
    context_->OMSetRenderTargets(1, &rtv_, nullptr);
    context_->ClearRenderTargetView(rtv_, clear);
    ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());

    swapchain_->Present(1, 0);
}

void Hud::drawOrb() {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 center(kWinW * 0.5f, 120.0f);

    const HudState st = state_.state.load();
    const OrbColor col = colorFor(st);
    const double now = std::chrono::duration<double>(
        std::chrono::steady_clock::now().time_since_epoch()).count() - start_time_;

    float pulse = 0.0f;
    switch (st) {
        case HudState::Listening:
            pulse = 0.5f + 0.5f * std::sin(float(now) * 6.0f);
            break;
        case HudState::Thinking:
            pulse = 0.5f + 0.5f * std::sin(float(now) * 10.0f);
            break;
        case HudState::Speaking:
            pulse = 0.5f + 0.5f * std::sin(float(now) * 14.0f);
            break;
        case HudState::Idle:
            pulse = 0.5f + 0.5f * std::sin(float(now) * 1.5f);
            break;
        case HudState::Disconnected:
            pulse = 0.15f;
            break;
    }

    const float baseR = kOrbRadius;
    ImGui::SetCursorPos(ImVec2(center.x - baseR, center.y - baseR));
    ImGui::InvisibleButton("##orb_cancel", ImVec2(baseR * 2.0f, baseR * 2.0f));
    const bool hovered = ImGui::IsItemHovered();
    const bool can_cancel = st == HudState::Listening
                          || st == HudState::Thinking
                          || st == HudState::Speaking;
    if (can_cancel && ImGui::IsItemClicked()) {
        ws_.sendCommand("cancel");
    }

    const int   rings = 5;
    for (int i = rings; i >= 1; --i) {
        float rad = baseR + i * 9.0f + pulse * 9.0f * (float)i;
        float a = (0.11f - i * 0.015f) * (0.5f + pulse * 0.5f);
        dl->AddCircleFilled(center, rad, toU32(col, a), 64);
    }

    // Inner core: slightly brighter, inner ring
    dl->AddCircleFilled(center, baseR, toU32(col, 0.4f + pulse * 0.3f), 64);
    {
        OrbColor dim{ col.r * 0.3f, col.g * 0.3f, col.b * 0.35f };
        dl->AddCircle(center, baseR * 0.88f, toU32(dim, 0.35f), 48, 1.0f);
    }
    {
        OrbColor edge{ col.r * 0.3f, col.g * 0.3f, col.b * 0.3f };
        dl->AddCircle(center, baseR, toU32(edge, 0.5f + pulse * 0.35f), 64, 2.2f);
    }
    if (hovered && can_cancel) {
        dl->AddCircle(center, baseR + 4.0f, IM_COL32(255, 255, 255, 210), 64, 2.0f);
        const float stop = 18.0f;
        dl->AddRectFilled(
            ImVec2(center.x - stop * 0.5f, center.y - stop * 0.5f),
            ImVec2(center.x + stop * 0.5f, center.y + stop * 0.5f),
            IM_COL32(255, 255, 255, 230),
            3.0f
        );
        ImGui::SetTooltip("Stop Jarvis");
    }

    const int arcs = 3;
    for (int i = 0; i < arcs; ++i) {
        float t = float(now) * (0.6f + i * 0.35f) + i * 1.7f;
        float a0 = t;
        float a1 = t + kPi * 0.4f;
        const int segs = 24;
        for (int ss = 0; ss < segs; ++ss) {
            float u0 = a0 + (a1 - a0) * (ss / float(segs));
            float u1 = a0 + (a1 - a0) * ((ss + 1) / float(segs));
            float r  = baseR - 12.0f - i * 9.0f;
            ImVec2 p0(center.x + std::cos(u0) * r, center.y + std::sin(u0) * r);
            ImVec2 p1(center.x + std::cos(u1) * r, center.y + std::sin(u1) * r);
            dl->AddLine(p0, p1, toU32(col, 0.9f - ss * 0.03f), 1.6f);
        }
    }
}

void Hud::drawTextPanel() {
    std::string status, user, transcript, reply;
    {
        std::lock_guard<std::mutex> lk(state_.text_mutex);
        status     = state_.status_line;
        user       = state_.last_user;
        transcript = state_.last_transcript;
        reply      = state_.last_reply;
    }

    const double now_sec = std::chrono::duration<double>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
    if (transcript != transcript_anim_source_) {
        transcript_anim_source_ = transcript;
        transcript_anim_cp_     = 0;
        transcript_anim_t0_       = now_sec;
    }
    const size_t ncp_total = utf8_count_cp(transcript);
    if (ncp_total > 0) {
        const double el = now_sec - transcript_anim_t0_;
        const size_t want =
            (size_t)std::min<double>((double)ncp_total, std::max(0.0, el * 42.0));
        transcript_anim_cp_ = want;
    } else {
        transcript_anim_cp_ = 0;
    }
    const std::string transcript_show = utf8_prefix_cp(transcript, transcript_anim_cp_);
    const bool typing = !transcript.empty() && transcript_anim_cp_ < ncp_total;

    const HudState st = state_.state.load();

    ImGui::SetCursorPos(ImVec2(0, (float)kTextPanelY));
    ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12, 10));
    const float panel_h = (float)(kWinH - kTextPanelY);
    ImGui::BeginChild("##panel", ImVec2((float)kWinW, panel_h), false);

    ImDrawList* pdl = ImGui::GetWindowDrawList();
    {
        const ImVec2 a = ImGui::GetWindowPos();
        const ImVec2 b = ImVec2(a.x + ImGui::GetWindowWidth(), a.y + ImGui::GetWindowHeight());
        pdl->AddRectFilled(a, b, IM_COL32(4, 12, 18, 140), 8.0f);
        pdl->AddRect(a, b, IM_COL32(0, 255, 200, 70), 8.0f, 0, 1.2f);
        // Inner “data” corners
        const float L = 6.0f;
        pdl->AddLine(ImVec2(a.x + 4, a.y + 8), ImVec2(a.x + 4 + L, a.y + 8), IM_COL32(255, 0, 200, 100), 1.0f);
        pdl->AddLine(ImVec2(b.x - 4 - L, b.y - 8), ImVec2(b.x - 4, b.y - 8), IM_COL32(0, 255, 220, 90), 1.0f);
    }

    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(8, 7));
    const float input_h = 48.0f;
    if (ImGui::BeginChild("##panel_body", ImVec2(0, panel_h - input_h), false)) {
        ImGui::Dummy(ImVec2(0, 2));

        ImGui::Indent(12);
        ImGui::TextColored(ImVec4(0.3f, 1.0f, 0.95f, 1.0f), "JARVIS");
        ImGui::SameLine(ImGui::GetWindowWidth() - 80);
        ImGui::TextColored(ImVec4(0.55f, 0.85f, 0.95f, 0.65f), ":: %s", toString(st));
        ImGui::Unindent(12);

        ImGui::Dummy(ImVec2(0, 2));
        ImGui::Indent(12);

        if (!transcript.empty()) {
            ImGui::TextColored(ImVec4(0.45f, 0.75f, 1.0f, 0.75f), "%s ::",
                               user.empty() ? "usr" : user.c_str());
            std::string display = transcript_show;
            if (typing) {
                display += '_';
            }
            ImGui::PushTextWrapPos(kWinW - 24);
            ImGui::TextColored(ImVec4(0.92f, 0.94f, 0.98f, 0.92f), "%s", display.c_str());
            ImGui::PopTextWrapPos();
        }
        if (!reply.empty()) {
            ImGui::Dummy(ImVec2(0, 6));
            ImGui::TextColored(ImVec4(0.2f, 0.95f, 0.75f, 0.85f), "OUT ::");
            ImGui::PushTextWrapPos(kWinW - 24);
            ImGui::TextColored(ImVec4(0.75f, 0.98f, 0.88f, 0.9f), "%s", reply.c_str());
            ImGui::PopTextWrapPos();
        }
        if (!status.empty()) {
            ImGui::Dummy(ImVec2(0, 4));
            ImGui::TextColored(ImVec4(0.5f, 0.65f, 0.78f, 0.55f), "%s", status.c_str());
        }
        ImGui::Unindent(12);
    }
    ImGui::EndChild();

    ImGui::Separator();
    ImGui::Indent(8);
    ImGui::SetNextItemWidth((float)kWinW - 110.0f);
    bool submit = ImGui::InputTextWithHint(
        "##prompt_input",
        "Type as guest... (/restart to restart backend)",
        prompt_buf_,
        sizeof(prompt_buf_),
        ImGuiInputTextFlags_EnterReturnsTrue
    );
    ImGui::SameLine();
    if (ImGui::Button("Send##prompt")) {
        submit = true;
    }
    ImGui::Unindent(8);
    if (submit) {
        std::string text(prompt_buf_);
        auto l = text.find_first_not_of(" \t\r\n");
        auto r = text.find_last_not_of(" \t\r\n");
        if (l != std::string::npos && r != std::string::npos) {
            text = text.substr(l, r - l + 1);
            if (text == "/restart") {
                state_.setStatus("Restarting backend...");
                if (restart_backend_) {
                    std::string msg = restart_backend_();
                    state_.setStatus(msg.empty() ? "Backend restart requested." : msg);
                } else {
                    state_.setStatus("Backend launcher unavailable.");
                }
            } else {
                ws_.sendPrompt(text);
            }
            prompt_buf_[0] = '\0';
        }
    }
    ImGui::PopStyleVar();
    ImGui::EndChild();
    ImGui::PopStyleVar(2);  // child rounding + child padding
}

void Hud::drawSettings() {
    std::string current_model, current_voice;
    std::string tts_provider, tts_active_provider;
    std::string eleven_voice_id, eleven_voice_name;
    std::string eleven_key_hint;
    bool eleven_has_key = false;
    bool speaker_enabled = true;
    float speaker_threshold = 0.75f;
    std::string owner;
    std::vector<std::string> models, voices, providers, profiles;
    std::vector<SharedState::UserSkillItem> user_skills;
    std::vector<SharedState::ElevenVoice> eleven_voices;
    int  enroll_n = 8;
    bool ollama_ready = false;
    {
        std::lock_guard<std::mutex> lk(state_.settings_mutex);
        ollama_ready        = state_.ollama_models_ready.load();
        enroll_n              = state_.enroll_sample_target;
        current_model       = state_.current_llm_model;
        current_voice       = state_.current_voice;
        tts_provider        = state_.tts_provider;
        tts_active_provider = state_.tts_active_provider;
        eleven_voice_id     = state_.elevenlabs_voice_id;
        eleven_voice_name   = state_.elevenlabs_voice_name;
        eleven_key_hint     = state_.elevenlabs_key_hint;
        eleven_has_key      = state_.elevenlabs_has_key;
        speaker_enabled     = state_.speaker_enabled;
        speaker_threshold   = state_.speaker_threshold;
        owner               = state_.owner;
        models              = state_.available_llm_models;
        voices              = state_.available_voices;
        providers           = state_.available_tts_providers;
        eleven_voices       = state_.available_elevenlabs_voices;
        profiles            = state_.available_profiles;
        user_skills         = state_.available_user_skills;
    }
    if (providers.empty()) providers = {"auto", "elevenlabs", "piper"};

    ImGui::SetNextWindowPos(ImVec2(0, 0), ImGuiCond_Always);
    ImGui::SetNextWindowSize(ImVec2((float)kWinW, (float)kWinH), ImGuiCond_Always);
    ImGuiWindowFlags flags = ImGuiWindowFlags_NoTitleBar
                           | ImGuiWindowFlags_NoResize
                           | ImGuiWindowFlags_NoMove
                           | ImGuiWindowFlags_NoCollapse
                           | ImGuiWindowFlags_NoSavedSettings
                           | ImGuiWindowFlags_AlwaysVerticalScrollbar;
    ImGui::PushStyleColor(ImGuiCol_WindowBg, kOverlayWindowBg);
    if (!ImGui::Begin("##jarvis_settings", nullptr, flags)) {
        ImGui::End();
        ImGui::PopStyleColor();
        return;
    }

    ImGui::Dummy(ImVec2(0, 4));
    ImGui::Indent(10);
    ImGui::TextColored(ImVec4(0.35f, 1.0f, 0.92f, 1.0f), "CONFIG // SYS");
    ImGui::SameLine(ImGui::GetWindowWidth() - 198);
    if (ImGui::Button("Close##set")) show_settings_ = false;
    ImGui::SameLine();
    if (ImGui::Button("Refresh##set")) ws_.requestSettings();
    ImGui::SameLine();
    if (ImGui::Button("Skills##set")) ws_.requestUserSkills();
    ImGui::Unindent(10);

    ImGui::SeparatorText("SECTIONS");
    ImGui::Indent(8);

    if (ImGui::CollapsingHeader("> LLM / Ollama",
            ImGuiTreeNodeFlags_DefaultOpen | ImGuiTreeNodeFlags_Framed)) {
        if (!ollama_ready) {
            ImGui::TextDisabled("Preparing (pull may take a while)...");
        } else if (models.empty()) {
            ImGui::TextColored(ImVec4(0.95f, 0.5f, 0.4f, 0.9f), "No models (start Ollama).");
        } else {
            ImGui::TextColored(ImVec4(0.35f, 0.9f, 0.55f, 0.9f), "OK — %d tag(s).", (int)models.size());
        }
        ImGui::SetNextItemWidth((float)kWinW - 28);
        if (!ollama_ready) {
            ImGui::BeginDisabled();
        }
        {
            const char* prev = ollama_ready
                ? (current_model.empty() ? "(select)" : current_model.c_str())
                : "…";
            if (ImGui::BeginCombo("##llm", prev)) {
                if (ollama_ready) {
                    if (models.empty()) {
                        ImGui::TextDisabled("—");
                    }
                    for (const auto& m : models) {
                        bool sel = (m == current_model);
                        if (ImGui::Selectable(m.c_str(), sel)) {
                            ws_.setLlmModel(m);
                        }
                        if (sel) ImGui::SetItemDefaultFocus();
                    }
                } else {
                    ImGui::TextDisabled("…");
                }
                ImGui::EndCombo();
            }
        }
        if (!ollama_ready) {
            ImGui::EndDisabled();
        }
    }

    if (ImGui::CollapsingHeader("> TTS", ImGuiTreeNodeFlags_DefaultOpen | ImGuiTreeNodeFlags_Framed)) {
        ImGui::TextDisabled("active: %s",
            tts_active_provider.empty() ? "?" : tts_active_provider.c_str());
        ImGui::SetNextItemWidth((float)kWinW - 28);
        if (ImGui::BeginCombo("##ttsprov",
                              tts_provider.empty() ? "auto" : tts_provider.c_str())) {
            for (const auto& p : providers) {
                bool sel = (p == tts_provider);
                if (ImGui::Selectable(p.c_str(), sel)) {
                    ws_.setTtsProvider(p);
                }
                if (sel) ImGui::SetItemDefaultFocus();
            }
            ImGui::EndCombo();
        }
        ImGui::Text("ElevenLabs key");
        ImGui::SetNextItemWidth((float)kWinW - 124);
        ImGui::InputTextWithHint("##elevenkey", "sk-...", eleven_key_buf_, sizeof(eleven_key_buf_),
                                 ImGuiInputTextFlags_Password);
        ImGui::SameLine();
        if (ImGui::Button("Save##k")) {
            ws_.setElevenlabsKey(eleven_key_buf_);
        }
        ImGui::SameLine();
        if (ImGui::Button("Clear##k")) {
            ws_.setElevenlabsKey("");
            eleven_key_buf_[0] = '\0';
        }
        if (eleven_has_key && !eleven_key_hint.empty()) {
            ImGui::TextDisabled("saved: %s", eleven_key_hint.c_str());
        }
        ImGui::SetNextItemWidth((float)kWinW - 100);
        {
            std::string label = eleven_voice_name.empty() ? eleven_voice_id : eleven_voice_name;
            if (label.empty()) {
                label = "(none)";
            }
            if (ImGui::BeginCombo("##elevenvoice", label.c_str())) {
                if (eleven_voices.empty()) {
                    ImGui::TextDisabled(eleven_has_key ? "Use Refresh." : "Add key first.");
                }
                for (const auto& v : eleven_voices) {
                    bool sel = (v.id == eleven_voice_id);
                    std::string line = v.name.empty() ? v.id : v.name;
                    if (ImGui::Selectable(line.c_str(), sel)) {
                        ws_.setElevenlabsVoice(v.id, v.name);
                    }
                    if (sel) ImGui::SetItemDefaultFocus();
                }
                ImGui::EndCombo();
            }
        }
        ImGui::SameLine();
        if (ImGui::Button("Go##ev")) {
            ws_.refreshElevenlabsVoices();
        }
        ImGui::SetNextItemWidth((float)kWinW - 28);
        if (ImGui::BeginCombo("##voice", current_voice.empty() ? "Piper" : current_voice.c_str())) {
            if (voices.empty()) {
                ImGui::TextDisabled("No piper/ voices.");
            }
            for (const auto& v : voices) {
                bool sel = (v == current_voice);
                if (ImGui::Selectable(v.c_str(), sel)) {
                    ws_.setVoice(v);
                }
                if (sel) ImGui::SetItemDefaultFocus();
            }
            ImGui::EndCombo();
        }
    }

    if (ImGui::CollapsingHeader("> Speaker", ImGuiTreeNodeFlags_Framed)) {
        {
            bool enabled = speaker_enabled;
            if (ImGui::Checkbox("ID speaker", &enabled)) {
                ws_.setSpeakerEnabled(enabled);
            }
        }
        ImGui::TextDisabled("lower = looser match, higher = stricter");
        ImGui::SetNextItemWidth((float)kWinW - 28);
        {
            float thr = speaker_threshold;
            ImGui::SliderFloat("##spkthr", &thr, 0.50f, 0.95f, "match %.2f",
                              ImGuiSliderFlags_AlwaysClamp);
            if (ImGui::IsItemDeactivatedAfterEdit()) {
                ws_.setSpeakerThreshold(thr);
            }
        }
        ImGui::SetNextItemWidth((float)kWinW - 28);
        {
            std::string olabel = owner.empty() ? "owner" : owner;
            if (ImGui::BeginCombo("##owner", olabel.c_str())) {
                if (profiles.empty()) {
                    ImGui::TextDisabled("enroll first");
                }
                for (const auto& p : profiles) {
                    bool sel = (p == owner);
                    if (ImGui::Selectable(p.c_str(), sel)) {
                        ws_.setOwner(p);
                    }
                    if (sel) ImGui::SetItemDefaultFocus();
                }
                ImGui::EndCombo();
            }
        }
        if (profiles.empty()) {
            ImGui::TextDisabled("— no profiles");
        } else {
            for (const auto& p : profiles) {
                ImGui::PushID(p.c_str());
                ImGui::BulletText("%s%s", p.c_str(), (p == owner) ? " *" : "");
                ImGui::SameLine((float)kWinW - 120);
                if (ImGui::Button("More")) {
                    ws_.enrollStart(p, true);
                }
                ImGui::SameLine();
                if (ImGui::Button("Del")) {
                    ws_.deleteProfile(p);
                }
                ImGui::PopID();
            }
        }
        bool enrolling = state_.enrolling.load();
        if (enrolling) {
            int c = state_.enroll_collected.load();
            int t = state_.enroll_target.load();
            std::string en;
            {
                std::lock_guard<std::mutex> lk(state_.text_mutex);
                en = state_.enroll_name;
            }
            const char* lab = state_.enroll_refine.load() ? "Refine" : "Enroll";
            ImGui::Text("%s %s: %d / %d", lab, en.c_str(), c, t);
            if (ImGui::Button("Cancel##enr")) {
                ws_.enrollCancel();
            }
        } else {
            ImGui::SetNextItemWidth((float)kWinW - 100);
            ImGui::InputTextWithHint("##enrollname", "name", enroll_name_buf_, sizeof(enroll_name_buf_));
            ImGui::SameLine();
            bool can_start = enroll_name_buf_[0] != '\0';
            if (!can_start) {
                ImGui::BeginDisabled();
            }
            if (ImGui::Button("Start##enr")) {
                ws_.enrollStart(enroll_name_buf_, false);
                enroll_name_buf_[0] = '\0';
            }
            if (!can_start) {
                ImGui::EndDisabled();
            }
            ImGui::TextDisabled("%d lines / session", enroll_n);
        }
    }

    if (ImGui::CollapsingHeader("> Custom Skills", ImGuiTreeNodeFlags_Framed)) {
        ImGui::TextDisabled("Loaded user-defined tools");
        if (ImGui::Button("Refresh##skills")) {
            ws_.requestUserSkills();
        }
        if (user_skills.empty()) {
            ImGui::TextDisabled("— none yet");
        } else {
            for (const auto& s : user_skills) {
                ImGui::PushID(s.name.c_str());
                ImGui::BulletText("%s", s.name.c_str());
                if (!s.source.empty()) {
                    ImGui::SameLine();
                    ImGui::TextDisabled("[%s]", s.source.c_str());
                }
                if (!s.description.empty()) {
                    ImGui::PushTextWrapPos((float)kWinW - 28);
                    ImGui::TextColored(ImVec4(0.75f, 0.82f, 0.90f, 0.85f),
                                       "%s", s.description.c_str());
                    ImGui::PopTextWrapPos();
                }
                ImGui::PopID();
            }
        }
    }

    ImGui::Unindent(8);
    ImGui::Spacing();
    ImGui::TextDisabled("F1 log  |  F2 settings  |  F3 patches  |  Ctrl+Space PTT  |  Ctrl+Q quit");
    drawVersionCorner();
    ImGui::End();
    ImGui::PopStyleColor();
}

void Hud::drawPatches() {
    std::vector<SharedState::PatchItem> patches;
    {
        std::lock_guard<std::mutex> lk(state_.patches_mutex);
        patches = state_.pending_patches;
    }

    ImGui::SetNextWindowPos(ImVec2(0, 0), ImGuiCond_Always);
    ImGui::SetNextWindowSize(ImVec2((float)kWinW, (float)kWinH), ImGuiCond_Always);
    ImGuiWindowFlags flags = ImGuiWindowFlags_NoTitleBar
                           | ImGuiWindowFlags_NoResize
                           | ImGuiWindowFlags_NoMove
                           | ImGuiWindowFlags_NoCollapse
                           | ImGuiWindowFlags_NoSavedSettings;
    ImGui::PushStyleColor(ImGuiCol_WindowBg, kOverlayWindowBg);
    if (!ImGui::Begin("##jarvis_patches", nullptr, flags)) {
        ImGui::End();
        ImGui::PopStyleColor();
        return;
    }

    ImGui::Dummy(ImVec2(0, 4));
    ImGui::Indent(12);
    ImGui::TextColored(ImVec4(1.0f, 0.45f, 0.9f, 0.95f), "PATCH // REVIEW");
    ImGui::SameLine(ImGui::GetWindowWidth() - 196);
    if (ImGui::Button("Close##p")) show_patches_ = false;
    ImGui::SameLine();
    if (ImGui::Button("Refresh##p")) ws_.requestPatches();
    ImGui::Unindent(12);

    ImGui::Dummy(ImVec2(0, 6));
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0, 4));

    if (patches.empty()) {
        ImGui::Indent(12);
        ImGui::TextWrapped(
            "No patches waiting. When Jarvis proposes changes to its own "
            "code they'll appear here for your approval.");
        ImGui::Unindent(12);
        drawVersionCorner();
        ImGui::End();
        ImGui::PopStyleColor();
        return;
    }

    ImGui::BeginChild("##patch_list", ImVec2(0, 0), false);
    for (auto& p : patches) {
        ImGui::PushID(p.id.c_str());
        ImGui::Indent(10);
        ImGui::TextColored(ImVec4(0.85f, 0.85f, 0.95f, 1.0f),
                           "%s", p.target.c_str());
        ImGui::PushTextWrapPos(kWinW - 20);
        ImGui::TextColored(ImVec4(0.75f, 0.75f, 0.8f, 0.9f),
                           "%s", p.description.c_str());
        ImGui::PopTextWrapPos();

        bool is_open = (selected_patch_id_ == p.id);
        if (ImGui::Button(is_open ? "hide diff" : "view diff")) {
            selected_patch_id_ = is_open ? std::string() : p.id;
        }
        ImGui::SameLine();
        if (ImGui::Button("approve")) {
            ws_.approvePatch(p.id);
        }
        ImGui::SameLine();
        if (ImGui::Button("reject")) {
            ws_.rejectPatch(p.id);
        }

        if (is_open) {
            ImGui::BeginChild(("##diff_" + p.id).c_str(),
                              ImVec2(0, 180), true,
                              ImGuiWindowFlags_HorizontalScrollbar);
            // Render the unified diff with red/green/context colouring.
            const std::string& d = p.diff;
            size_t i = 0;
            while (i < d.size()) {
                size_t j = d.find('\n', i);
                std::string line = d.substr(i, j == std::string::npos ? std::string::npos : j - i);
                ImVec4 col(0.8f, 0.8f, 0.85f, 1.0f);
                if (!line.empty()) {
                    if (line[0] == '+' && line.rfind("+++", 0) != 0) col = ImVec4(0.4f, 0.9f, 0.5f, 1.0f);
                    else if (line[0] == '-' && line.rfind("---", 0) != 0) col = ImVec4(1.0f, 0.5f, 0.5f, 1.0f);
                    else if (line[0] == '@') col = ImVec4(0.7f, 0.7f, 1.0f, 1.0f);
                }
                ImGui::TextColored(col, "%s", line.c_str());
                if (j == std::string::npos) break;
                i = j + 1;
            }
            ImGui::EndChild();
        }

        ImGui::Unindent(10);
        ImGui::Separator();
        ImGui::PopID();
    }
    ImGui::EndChild();
    drawVersionCorner();
    ImGui::End();
    ImGui::PopStyleColor();
}

void Hud::refreshLogs() {
    if (log_path_.empty()) return;
    std::error_code ec;
    std::filesystem::path p(log_path_);
    if (!std::filesystem::exists(p, ec)) {
        log_buffer_ = "(no log file yet at " + log_path_ + ")";
        return;
    }
    auto size = std::filesystem::file_size(p, ec);
    if (ec) { log_buffer_ = "(failed to stat log)"; return; }

    // Tail the last ~128 KB - the backend log can grow large and we only
    // care about what happened recently.
    constexpr std::uintmax_t kTail = 128 * 1024;
    std::ifstream in(p, std::ios::binary);
    if (!in) { log_buffer_ = "(failed to open log)"; return; }
    if (size > kTail) {
        in.seekg(static_cast<std::streamoff>(size - kTail));
        // Skip to the next newline to avoid showing a half-chopped line.
        std::string skip;
        std::getline(in, skip);
    }
    std::string content((std::istreambuf_iterator<char>(in)),
                         std::istreambuf_iterator<char>());
    log_buffer_ = std::move(content);
    if (log_buffer_.empty()) log_buffer_ = "(log is empty)";
}

void Hud::drawLogs() {
    // Auto-refresh every 500 ms while the panel is open so live output
    // from the backend keeps streaming in.
    double now = ImGui::GetTime();
    if (now - log_last_read_ > 0.5) {
        refreshLogs();
        log_last_read_ = now;
    }

    ImGui::SetNextWindowPos(ImVec2(0, 0), ImGuiCond_Always);
    ImGui::SetNextWindowSize(ImVec2((float)kWinW, (float)kWinH), ImGuiCond_Always);
    ImGuiWindowFlags flags = ImGuiWindowFlags_NoTitleBar
                           | ImGuiWindowFlags_NoResize
                           | ImGuiWindowFlags_NoMove
                           | ImGuiWindowFlags_NoCollapse
                           | ImGuiWindowFlags_NoSavedSettings;
    ImGui::PushStyleColor(ImGuiCol_WindowBg, kOverlayWindowBg);
    if (!ImGui::Begin("##jarvis_logs", nullptr, flags)) {
        ImGui::End();
        ImGui::PopStyleColor();
        return;
    }

    ImGui::Dummy(ImVec2(0, 4));
    ImGui::Indent(12);
    ImGui::TextColored(ImVec4(0.4f, 0.95f, 0.9f, 0.95f), "LOGS // BACKEND");
    ImGui::SameLine(ImGui::GetWindowWidth() - 188);
    ImGui::Checkbox("tail", &logs_autoscroll_);
    ImGui::SameLine();
    if (ImGui::Button("Close##L")) show_logs_ = false;
    ImGui::Unindent(12);

    if (!log_path_.empty()) {
        ImGui::Indent(12);
        ImGui::TextColored(ImVec4(0.55f, 0.55f, 0.60f, 0.8f), "%s", log_path_.c_str());
        ImGui::Unindent(12);
    }

    ImGui::Dummy(ImVec2(0, 2));
    ImGui::Separator();

    ImGui::BeginChild("##logbody", ImVec2(0, 0), false,
                      ImGuiWindowFlags_HorizontalScrollbar);
    ImGui::PushFont(ImGui::GetFont());
    // Render line-by-line so long logs don't blow past ImGui's per-call
    // text limit, and so we can colour-code warnings/errors.
    const std::string& d = log_buffer_;
    size_t i = 0;
    while (i < d.size()) {
        size_t j = d.find('\n', i);
        std::string line = d.substr(
            i, j == std::string::npos ? std::string::npos : j - i);
        ImVec4 col(0.85f, 0.85f, 0.90f, 1.0f);
        // Cheap severity heuristics - matches the default logging format.
        if (line.find("ERROR")   != std::string::npos ||
            line.find("Error")   != std::string::npos ||
            line.find("Traceback") != std::string::npos) {
            col = ImVec4(1.0f, 0.55f, 0.55f, 1.0f);
        } else if (line.find("WARNING") != std::string::npos ||
                   line.find("warn")    != std::string::npos) {
            col = ImVec4(1.0f, 0.85f, 0.45f, 1.0f);
        } else if (line.find("INFO") != std::string::npos) {
            col = ImVec4(0.75f, 0.85f, 1.0f, 1.0f);
        }
        ImGui::TextColored(col, "%s", line.c_str());
        if (j == std::string::npos) break;
        i = j + 1;
    }
    ImGui::PopFont();
    if (logs_autoscroll_) ImGui::SetScrollHereY(1.0f);
    ImGui::EndChild();

    drawVersionCorner();
    ImGui::End();
    ImGui::PopStyleColor();
}

void Hud::drawVersionCorner() {
    char label[64];
    std::snprintf(label, sizeof(label), "v%s", JARVIS_HUD_VERSION_STR);
    const ImVec2 ts = ImGui::CalcTextSize(label);
    const float   pad = 7.0f;
    const ImVec2  wp  = ImGui::GetWindowPos();
    const ImVec2  ws  = ImGui::GetWindowSize();
    const ImVec2  p(
        wp.x + ws.x - ts.x - pad, wp.y + ws.y - ts.y - pad);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    // Subtle shadow then text (futuristic HUD readout)
    dl->AddText(
        ImVec2(p.x + 1, p.y + 1), IM_COL32(0, 0, 0, 100), label);
    dl->AddText(
        p, IM_COL32(0, 255, 200, 220), label);
}

void Hud::shutdown() {
    if (!hwnd_) return;
    if (!layout_path_.empty()) {
        saveWindowLayout(hwnd_, fs::path(layout_path_));
    }
    ImGui_ImplDX11_Shutdown();
    ImGui_ImplWin32_Shutdown();
    ImGui::DestroyContext();
    cleanupDeviceD3D();
    DestroyWindow(hwnd_);
    UnregisterClassW(kClassName, hinst_);
    hwnd_ = nullptr;
}

} // namespace jarvis

#pragma once

#include "State.h"

#include <string>
#include <windows.h>

struct ID3D11Device;
struct ID3D11DeviceContext;
struct IDXGISwapChain;
struct ID3D11RenderTargetView;

namespace jarvis {

class WsClient;

class Hud {
public:
    Hud(SharedState& state, WsClient& ws, std::string log_path = {});
    ~Hud();

    bool init();
    void run();
    void shutdown();

    LRESULT handleMessage(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp);

private:
    void render();
    void drawOrb();
    void drawTextPanel();
    void drawSettings();
    void drawPatches();
    void drawLogs();
    void drawVersionCorner();
    void refreshLogs();

    bool createDeviceD3D(HWND hwnd);
    void cleanupDeviceD3D();
    void createRenderTarget();
    void cleanupRenderTarget();

    /// Drop topmost/tool-window bits so Windows shows us on the taskbar, then minimize.
    void minimizeToTaskbar();
    /// After restore from taskbar, go back to floating always-on-top HUD behavior.
    void restoreOverlayWindowStyles(HWND hwnd);

    SharedState& state_;
    WsClient&    ws_;

    HWND  hwnd_  = nullptr;
    HINSTANCE hinst_ = nullptr;

    ID3D11Device*           device_      = nullptr;
    ID3D11DeviceContext*    context_     = nullptr;
    IDXGISwapChain*         swapchain_   = nullptr;
    ID3D11RenderTargetView* rtv_         = nullptr;

    UINT resize_w_ = 0;
    UINT resize_h_ = 0;

    double start_time_ = 0.0;
    bool   push_to_talk_down_ = false;
    bool   f1_down_ = false;
    bool   f2_down_ = false;
    bool   f3_down_ = false;
    bool   show_settings_ = false;
    bool   show_patches_  = false;
    bool   show_logs_     = false;
    bool   logs_autoscroll_ = true;
    std::string selected_patch_id_;
    char   eleven_key_buf_[256] = {0};
    char   enroll_name_buf_[64] = {0};
    char   prompt_buf_[768] = {0};
    std::string log_path_;
    std::string layout_path_;
    std::string log_buffer_;
    double      log_last_read_ = 0.0;
    bool   running_ = true;

    /// For UTF-8 typing animation on the user transcript line.
    std::string transcript_anim_source_;
    size_t      transcript_anim_cp_ = 0;
    double      transcript_anim_t0_  = 0.0;
};

} // namespace jarvis

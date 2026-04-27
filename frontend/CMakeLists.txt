cmake_minimum_required(VERSION 3.20)
project(jarvis_hud LANGUAGES CXX C)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(MSVC)
    add_compile_options(/W4 /permissive-)
    add_definitions(-DNOMINMAX -D_CRT_SECURE_NO_WARNINGS)
else()
    add_compile_options(-Wall -Wextra -Wpedantic)
endif()

include(FetchContent)
set(FETCHCONTENT_QUIET OFF)

# ------------------------------------------------------------------
# Dear ImGui (Win32 + D3D11 backends)
# ------------------------------------------------------------------
FetchContent_Declare(
    imgui
    GIT_REPOSITORY https://github.com/ocornut/imgui.git
    GIT_TAG v1.91.1
)
FetchContent_MakeAvailable(imgui)

set(IMGUI_SRC
    ${imgui_SOURCE_DIR}/imgui.cpp
    ${imgui_SOURCE_DIR}/imgui_draw.cpp
    ${imgui_SOURCE_DIR}/imgui_tables.cpp
    ${imgui_SOURCE_DIR}/imgui_widgets.cpp
    ${imgui_SOURCE_DIR}/imgui_demo.cpp
    ${imgui_SOURCE_DIR}/backends/imgui_impl_win32.cpp
    ${imgui_SOURCE_DIR}/backends/imgui_impl_dx11.cpp
)
add_library(imgui STATIC ${IMGUI_SRC})
target_include_directories(imgui PUBLIC
    ${imgui_SOURCE_DIR}
    ${imgui_SOURCE_DIR}/backends
)
if(WIN32)
    target_link_libraries(imgui PUBLIC d3d11 dxgi d3dcompiler)
endif()

# ------------------------------------------------------------------
# ixwebsocket (WebSocket client)
# USE_ZLIB is forced OFF so we don't drag zlib1.dll along for the ride -
# permessage-deflate is pointless for a localhost connection anyway.
# ------------------------------------------------------------------
set(USE_TLS OFF CACHE BOOL "" FORCE)
set(USE_ZLIB OFF CACHE BOOL "" FORCE)
set(IXWEBSOCKET_INSTALL OFF CACHE BOOL "" FORCE)
FetchContent_Declare(
    ixwebsocket
    GIT_REPOSITORY https://github.com/machinezone/IXWebSocket.git
    GIT_TAG v11.4.5
)
FetchContent_MakeAvailable(ixwebsocket)

# ------------------------------------------------------------------
# nlohmann/json (header-only)
# ------------------------------------------------------------------
FetchContent_Declare(
    nlohmann_json
    GIT_REPOSITORY https://github.com/nlohmann/json.git
    GIT_TAG v3.11.3
)
FetchContent_MakeAvailable(nlohmann_json)

# ------------------------------------------------------------------
# HUD target
# ------------------------------------------------------------------
option(JARVIS_CONSOLE "Build HUD as console app for logs" OFF)
set(JARVIS_SOURCES
    src/main.cpp
    src/Hud.cpp
    src/WsClient.cpp
    src/BackendLauncher.cpp
)
if(JARVIS_CONSOLE OR NOT WIN32)
    add_executable(jarvis_hud ${JARVIS_SOURCES})
else()
    add_executable(jarvis_hud WIN32 ${JARVIS_SOURCES})
endif()
set_target_properties(jarvis_hud PROPERTIES OUTPUT_NAME "jarvis")
# Shown in the HUD (bottom-right) and should match the Windows installer
# (installer/jarvis.iss) when you cut a release. CACHE FORCE so a stale
# JARVIS_HUD_VERSION from an older first configure cannot stick at e.g. 0.2.0.
set(JARVIS_HUD_VERSION "0.2.5" CACHE STRING "HUD / release version label" FORCE)
target_compile_definitions(jarvis_hud PRIVATE
    "JARVIS_HUD_VERSION_STR=\"${JARVIS_HUD_VERSION}\""
)
target_include_directories(jarvis_hud PRIVATE src)
target_link_libraries(jarvis_hud PRIVATE
    imgui
    ixwebsocket
    nlohmann_json::nlohmann_json
)

if(WIN32)
    target_link_libraries(jarvis_hud PRIVATE
        d3d11 dxgi d3dcompiler
        dwmapi user32 gdi32 imm32
        ws2_32
    )
endif()

# MinGW: statically link C/C++ runtime so the exe is standalone
if(MINGW)
    target_link_options(jarvis_hud PRIVATE
        -static-libgcc -static-libstdc++ -static
    )
endif()

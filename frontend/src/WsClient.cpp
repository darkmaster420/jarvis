#include "WsClient.h"

#include <nlohmann/json.hpp>

#include <cstdio>

namespace jarvis {

using nlohmann::json;

namespace {
HudState stateFromString(const std::string& s) {
    if (s == "idle")      return HudState::Idle;
    if (s == "listening") return HudState::Listening;
    if (s == "thinking")  return HudState::Thinking;
    if (s == "speaking")  return HudState::Speaking;
    return HudState::Idle;
}

void applySettings(SharedState& state, const json& s) {
    std::lock_guard<std::mutex> lk(state.settings_mutex);
    if (s.contains("current")) {
        const auto& c = s["current"];
        state.current_llm_model       = c.value("llm_model", state.current_llm_model);
        state.current_voice           = c.value("voice", state.current_voice);
        state.tts_provider            = c.value("tts_provider", state.tts_provider);
        state.tts_active_provider     = c.value("tts_active_provider",
                                                state.tts_active_provider);
        state.elevenlabs_voice_id     = c.value("elevenlabs_voice_id",
                                                state.elevenlabs_voice_id);
        state.elevenlabs_voice_name   = c.value("elevenlabs_voice_name",
                                                state.elevenlabs_voice_name);
        state.elevenlabs_model_id     = c.value("elevenlabs_model_id",
                                                state.elevenlabs_model_id);
        state.elevenlabs_key_hint     = c.value("elevenlabs_api_key_hint",
                                                state.elevenlabs_key_hint);
        state.elevenlabs_has_key      = c.value("elevenlabs_has_key",
                                                state.elevenlabs_has_key);
        state.speaker_enabled         = c.value("speaker_enabled",
                                                state.speaker_enabled);
        state.speaker_threshold       = c.value("speaker_threshold",
                                                state.speaker_threshold);
        state.owner                   = c.value("owner", state.owner);
        if (c.contains("enroll_samples")) {
            state.enroll_sample_target  = c.value("enroll_samples", 8);
        }
    }
    if (s.contains("available")) {
        const auto& a = s["available"];
        if (a.contains("llm_models") && a["llm_models"].is_array()) {
            state.available_llm_models.clear();
            for (const auto& m : a["llm_models"]) {
                state.available_llm_models.push_back(m.get<std::string>());
            }
        }
        if (a.contains("voices") && a["voices"].is_array()) {
            state.available_voices.clear();
            for (const auto& v : a["voices"]) {
                state.available_voices.push_back(v.get<std::string>());
            }
        }
        if (a.contains("tts_providers") && a["tts_providers"].is_array()) {
            state.available_tts_providers.clear();
            for (const auto& p : a["tts_providers"]) {
                state.available_tts_providers.push_back(p.get<std::string>());
            }
        }
        if (a.contains("profiles") && a["profiles"].is_array()) {
            state.available_profiles.clear();
            for (const auto& p : a["profiles"]) {
                state.available_profiles.push_back(p.get<std::string>());
            }
        }
        if (a.contains("elevenlabs_voices") && a["elevenlabs_voices"].is_array()) {
            state.available_elevenlabs_voices.clear();
            for (const auto& v : a["elevenlabs_voices"]) {
                SharedState::ElevenVoice ev;
                ev.id       = v.value("id", "");
                ev.name     = v.value("name", "");
                ev.category = v.value("category", "");
                if (!ev.id.empty()) {
                    state.available_elevenlabs_voices.push_back(std::move(ev));
                }
            }
        }
    }
    if (s.contains("ollama") && s["ollama"].is_object()) {
        const auto& o = s["ollama"];
        if (o.contains("ready") && o["ready"].is_boolean()) {
            state.ollama_models_ready.store(o["ready"].get<bool>());
        }
    } else {
        // Older backend: no `ollama` block — treat as ready so the HUD is usable.
        state.ollama_models_ready.store(true);
    }
}
} // namespace

WsClient::WsClient(SharedState& state, std::string url)
    : state_(state), url_(std::move(url)) {
    ws_.setUrl(url_);
    ws_.setPingInterval(20);
    ws_.enableAutomaticReconnection();
    ws_.setMinWaitBetweenReconnectionRetries(500);
    ws_.setMaxWaitBetweenReconnectionRetries(5000);

    ws_.setOnMessageCallback([this](const ix::WebSocketMessagePtr& msg) {
        onMessage(msg);
    });
}

WsClient::~WsClient() { stop(); }

void WsClient::start() {
    state_.setStatus("Connecting to " + url_ + " ...");
    ws_.start();
}

void WsClient::stop() {
    ws_.stop();
}

void WsClient::sendCommand(const std::string& cmd) {
    json j = {{"cmd", cmd}};
    sendRaw(j.dump());
}

void WsClient::sendRaw(const std::string& payload) {
    auto info = ws_.send(payload);
    (void)info;
}

void WsClient::sendPrompt(const std::string& text) {
    json j = {
        {"cmd", "prompt"},
        {"user", "guest"},
        {"text", text},
    };
    sendRaw(j.dump());
}

void WsClient::setLlmModel(const std::string& model) {
    json j = {{"cmd", "set_llm_model"}, {"model", model}};
    sendRaw(j.dump());
}

void WsClient::setVoice(const std::string& voice) {
    json j = {{"cmd", "set_voice"}, {"voice", voice}};
    sendRaw(j.dump());
}

void WsClient::setTtsProvider(const std::string& provider) {
    json j = {{"cmd", "set_tts_provider"}, {"provider", provider}};
    sendRaw(j.dump());
}

void WsClient::setElevenlabsKey(const std::string& key) {
    json j = {{"cmd", "set_elevenlabs_key"}, {"key", key}};
    sendRaw(j.dump());
}

void WsClient::setElevenlabsVoice(const std::string& id, const std::string& name) {
    json j = {{"cmd", "set_elevenlabs_voice"},
              {"voice_id", id},
              {"voice_name", name}};
    sendRaw(j.dump());
}

void WsClient::refreshElevenlabsVoices() {
    sendCommand("refresh_elevenlabs_voices");
}

void WsClient::setSpeakerEnabled(bool enabled) {
    json j = {{"cmd", "set_speaker_enabled"}, {"value", enabled}};
    sendRaw(j.dump());
}

void WsClient::setSpeakerThreshold(float value) {
    json j = {{"cmd", "set_speaker_threshold"}, {"value", value}};
    sendRaw(j.dump());
}

void WsClient::setOwner(const std::string& name) {
    json j = {{"cmd", "set_owner"}, {"owner", name}};
    sendRaw(j.dump());
}

void WsClient::deleteProfile(const std::string& name) {
    json j = {{"cmd", "delete_profile"}, {"name", name}};
    sendRaw(j.dump());
}

void WsClient::enrollStart(const std::string& name, bool refine) {
    json j = {{"cmd", "enroll_start"},
              {"name", name},
              {"refine", refine}};
    sendRaw(j.dump());
}

void WsClient::enrollCancel() {
    sendCommand("enroll_cancel");
}

void WsClient::requestSettings() {
    sendCommand("list_settings");
}

void WsClient::requestPatches() {
    sendCommand("list_patches");
}

void WsClient::approvePatch(const std::string& id) {
    json j = {{"cmd", "approve_patch"}, {"id", id}};
    sendRaw(j.dump());
}

void WsClient::rejectPatch(const std::string& id) {
    json j = {{"cmd", "reject_patch"}, {"id", id}};
    sendRaw(j.dump());
}

void WsClient::onMessage(const ix::WebSocketMessagePtr& msg) {
    switch (msg->type) {
        case ix::WebSocketMessageType::Open:
            state_.connected = true;
            state_.state     = HudState::Idle;
            state_.setStatus("Connected.");
            break;
        case ix::WebSocketMessageType::Close:
            state_.connected = false;
            state_.state     = HudState::Disconnected;
            state_.setStatus("Disconnected. Reconnecting...");
            break;
        case ix::WebSocketMessageType::Error:
            state_.connected = false;
            state_.state     = HudState::Disconnected;
            state_.setStatus("Connection error: " + msg->errorInfo.reason);
            break;
        case ix::WebSocketMessageType::Message:
            dispatch(msg->str);
            break;
        default:
            break;
    }
}

void WsClient::dispatch(const std::string& payload) {
    json j;
    try {
        j = json::parse(payload);
    } catch (...) {
        return;
    }
    const std::string ev = j.value("event", "");
    if (ev == "hello") {
        state_.state = stateFromString(j.value("state", "idle"));
        state_.muted = j.value("muted", false);
        if (j.contains("profiles") && j["profiles"].is_array()) {
            std::lock_guard<std::mutex> lk(state_.settings_mutex);
            state_.available_profiles.clear();
            for (const auto& p : j["profiles"]) {
                state_.available_profiles.push_back(p.get<std::string>());
            }
        }
        if (j.contains("settings")) applySettings(state_, j["settings"]);
        requestPatches();
    } else if (ev == "profiles") {
        std::lock_guard<std::mutex> lk(state_.settings_mutex);
        state_.available_profiles.clear();
        if (j.contains("items") && j["items"].is_array()) {
            for (const auto& p : j["items"]) {
                state_.available_profiles.push_back(p.get<std::string>());
            }
        }
    } else if (ev == "settings") {
        applySettings(state_, j);
    } else if (ev == "state") {
        state_.state = stateFromString(j.value("state", "idle"));
    } else if (ev == "wake") {
        state_.setStatus("Wake word detected");
    } else if (ev == "listening") {
        state_.state = HudState::Listening;
    } else if (ev == "transcript") {
        state_.setTranscript(j.value("text", ""), j.value("user", "guest"));
        state_.setStatus("");
    } else if (ev == "narration_start") {
        // Spoken "thinking" line while the model runs; keep orb in thinking.
        state_.setStatus(j.value("text", ""));
    } else if (ev == "narration_end") {
        state_.setStatus("");
    } else if (ev == "reply") {
        state_.setReply(j.value("text", ""));
    } else if (ev == "speaking_start") {
        state_.state = HudState::Speaking;
    } else if (ev == "speaking_end") {
        state_.state = HudState::Idle;
    } else if (ev == "cancelled") {
        state_.setStatus("Stopped.");
    } else if (ev == "muted") {
        state_.muted = j.value("value", false);
    } else if (ev == "enroll_progress") {
        int c = j.value("collected", 0);
        int t = j.value("target", 5);
        std::string n = j.value("name", "");
        bool vref     = j.value("refine", false);
        state_.enrolling        = true;
        state_.enroll_refine    = vref;
        state_.enroll_collected = c;
        state_.enroll_target    = t;
        {
            std::lock_guard<std::mutex> lk(state_.text_mutex);
            state_.enroll_name = n;
        }
        state_.setStatus(
            (vref ? "Refining " : "Enrolling ") + n + ": sample "
            + std::to_string(c) + " of " + std::to_string(t)
        );
    } else if (ev == "enroll_done") {
        state_.enrolling        = false;
        state_.enroll_refine    = false;
        state_.enroll_collected = 0;
        bool ok  = j.value("ok", false);
        bool vfr = j.value("refine", false);
        std::string n = j.value("name", "");
        if (ok) {
            state_.setStatus(vfr ? ("Voice profile updated for " + n + ".")
                                 : ("Enrolled " + n + "."));
        } else {
            state_.setStatus("Enrollment failed for " + n + ".");
        }
    } else if (ev == "enroll_cancelled") {
        state_.enrolling        = false;
        state_.enroll_refine    = false;
        state_.enroll_collected = 0;
        state_.setStatus("Enrollment cancelled.");
    } else if (ev == "patches") {
        std::lock_guard<std::mutex> lk(state_.patches_mutex);
        state_.pending_patches.clear();
        if (j.contains("items") && j["items"].is_array()) {
            for (const auto& it : j["items"]) {
                SharedState::PatchItem p;
                p.id          = it.value("id", "");
                p.target      = it.value("target", "");
                p.description = it.value("description", "");
                p.diff        = it.value("diff", "");
                p.created     = it.value("created", 0.0);
                if (!p.id.empty()) state_.pending_patches.push_back(std::move(p));
            }
        }
    } else if (ev == "patch_applied") {
        state_.setStatus("Patch applied: " + j.value("target", ""));
    } else if (ev == "error") {
        state_.setStatus("Error: " + j.value("message", ""));
    }
}

} // namespace jarvis

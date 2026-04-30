#pragma once

#include <atomic>
#include <mutex>
#include <string>
#include <vector>

namespace jarvis {

enum class HudState {
    Disconnected,
    Idle,
    Listening,
    Thinking,
    Speaking,
};

inline const char* toString(HudState s) {
    switch (s) {
        case HudState::Disconnected: return "disconnected";
        case HudState::Idle:         return "idle";
        case HudState::Listening:    return "listening";
        case HudState::Thinking:     return "thinking";
        case HudState::Speaking:     return "speaking";
    }
    return "?";
}

struct SharedState {
    std::atomic<HudState> state{HudState::Disconnected};
    std::atomic<bool>     muted{false};
    std::atomic<bool>     connected{false};

    std::mutex text_mutex;
    std::string last_transcript;
    std::string last_reply;
    std::string last_user;
    std::string status_line;

    struct ElevenVoice {
        std::string id;
        std::string name;
        std::string category;
    };

    std::mutex settings_mutex;
    std::string current_llm_model;
    std::string current_voice;
    std::string tts_provider          = "auto";          // requested
    std::string tts_active_provider   = "piper";         // what's live now
    std::string elevenlabs_voice_id;
    std::string elevenlabs_voice_name;
    std::string elevenlabs_model_id;
    /// Short masked hint from the backend (e.g. "sk_1...abcd") so the HUD can
    /// show that a key is configured after restart without echoing the secret.
    std::string elevenlabs_key_hint;
    bool        elevenlabs_has_key    = false;
    bool        speaker_enabled       = true;
    float       speaker_threshold     = 0.75f;
    std::string owner                 = "owner";
    int         enroll_sample_target  = 8;   // from config; phrases per session
    /// After backend Ollama bootstrap (start / pull) finishes. Default false until
    /// a settings snapshot (older servers omit `ollama` and we set true in applySettings).
    std::atomic<bool>            ollama_models_ready{false};
    std::vector<std::string>     available_llm_models;
    std::vector<std::string>     available_voices;
    std::vector<std::string>     available_tts_providers;
    std::vector<ElevenVoice>     available_elevenlabs_voices;
    std::vector<std::string>     available_profiles;
    struct UserSkillItem {
        std::string name;
        std::string description;
        std::string source;
    };
    std::vector<UserSkillItem>   available_user_skills;

    std::atomic<bool> enrolling{false};
    std::atomic<bool> enroll_refine{false};
    std::atomic<int>  enroll_collected{0};
    std::atomic<int>  enroll_target{5};
    std::string       enroll_name;

    struct PatchItem {
        std::string id;
        std::string target;
        std::string description;
        std::string diff;
        double      created = 0.0;
    };
    std::mutex patches_mutex;
    std::vector<PatchItem> pending_patches;

    void setTranscript(std::string t, std::string user) {
        std::lock_guard<std::mutex> lk(text_mutex);
        last_transcript = std::move(t);
        last_user       = std::move(user);
        // New user turn started: drop stale assistant output immediately.
        last_reply.clear();
    }
    void setReply(std::string t) {
        std::lock_guard<std::mutex> lk(text_mutex);
        last_reply = std::move(t);
    }
    void clearForNewPrompt() {
        std::lock_guard<std::mutex> lk(text_mutex);
        last_transcript.clear();
        last_reply.clear();
        status_line.clear();
    }
    void setStatus(std::string t) {
        std::lock_guard<std::mutex> lk(text_mutex);
        status_line = std::move(t);
    }
};

} // namespace jarvis

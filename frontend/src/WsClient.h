#pragma once

#include "State.h"

#include <ixwebsocket/IXWebSocket.h>

#include <memory>
#include <string>

namespace jarvis {

class WsClient {
public:
    WsClient(SharedState& state, std::string url);
    ~WsClient();

    void start();
    void stop();

    void sendCommand(const std::string& cmd);
    void sendRaw(const std::string& json);
    void setLlmModel(const std::string& model);
    void setVoice(const std::string& voice);
    void setTtsProvider(const std::string& provider);
    void setElevenlabsKey(const std::string& key);
    void setElevenlabsVoice(const std::string& id, const std::string& name);
    void refreshElevenlabsVoices();
    void setSpeakerEnabled(bool enabled);
    void setSpeakerThreshold(float value);
    void setOwner(const std::string& name);
    void deleteProfile(const std::string& name);
    void enrollStart(const std::string& name, bool refine = false);
    void enrollCancel();
    void requestSettings();
    void requestPatches();
    void approvePatch(const std::string& id);
    void rejectPatch(const std::string& id);

private:
    void onMessage(const ix::WebSocketMessagePtr& msg);
    void dispatch(const std::string& payload);

    SharedState&       state_;
    std::string        url_;
    ix::WebSocket      ws_;
};

} // namespace jarvis

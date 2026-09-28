#include "client.hpp"

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <mutex>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include <curl/curl.h>

#include "jsonvalue.hpp"

namespace engine {
namespace {

std::once_flag g_curl_once;

void ensure_curl() {
    std::call_once(g_curl_once, [] { curl_global_init(CURL_GLOBAL_DEFAULT); });
}

void apply_auth(CURL* curl, const std::string& username, const std::string& password) {
    // Aiohttp only attached credentials when a password was configured.
    if (password.empty()) {
        return;
    }
    curl_easy_setopt(curl, CURLOPT_HTTPAUTH, static_cast<long>(CURLAUTH_BASIC));
    curl_easy_setopt(curl, CURLOPT_USERNAME, username.c_str());
    curl_easy_setopt(curl, CURLOPT_PASSWORD, password.c_str());
}

std::size_t collect_body(char* data, std::size_t size, std::size_t nmemb, void* userdata) {
    auto* body = static_cast<std::string*>(userdata);
    const std::size_t total = size * nmemb;
    body->append(data, total);
    return total;
}

bool present(const std::optional<std::string>& value) {
    return value.has_value() && !value->empty();
}

// (await ...)["data"]
Json require_data(const Json& response) {
    if (!response.is_object()) {
        throw std::runtime_error("opencode response was not an object");
    }
    const auto it = response.find("data");
    if (it == response.end()) {
        throw std::runtime_error("opencode response had no data field");
    }
    return *it;
}

// (await ... or {}).get("data")
std::optional<Json> optional_data(const Json& response) {
    if (!response.is_object()) {
        return std::nullopt;
    }
    const auto it = response.find("data");
    if (it == response.end()) {
        return std::nullopt;
    }
    return *it;
}

// (await ... or {}).get("data", [])
std::vector<Json> list_from_data(const Json& response) {
    if (!response.is_object()) {
        return {};
    }
    const auto it = response.find("data");
    if (it == response.end() || !it->is_array()) {
        return {};
    }
    return std::vector<Json>(it->begin(), it->end());
}

}  // namespace

OpenCodeClient::OpenCodeClient(std::string url, std::string username, std::string password,
                               double timeout)
    : base_(std::move(url)),
      username_(std::move(username)),
      password_(std::move(password)),
      timeout_(timeout) {
    while (!base_.empty() && base_.back() == '/') {
        base_.pop_back();
    }
}

OpenCodeClient::~OpenCodeClient() { close(); }

Json OpenCodeClient::request(const std::string& method, const std::string& path, const Query& query,
                             const std::optional<Json>& body) {
    ensure_curl();

    CURL* curl = curl_easy_init();
    if (curl == nullptr) {
        throw std::runtime_error("curl_easy_init failed");
    }

    std::string url = base_ + path;
    if (!query.empty()) {
        url += "?";
        for (std::size_t i = 0; i < query.size(); ++i) {
            if (i > 0) {
                url += "&";
            }
            char* escaped = curl_easy_escape(curl, query[i].second.c_str(),
                                             static_cast<int>(query[i].second.size()));
            url += query[i].first + "=" + (escaped != nullptr ? escaped : "");
            if (escaped != nullptr) {
                curl_free(escaped);
            }
        }
    }

    const std::string payload_text = body.has_value() ? body->dump() : std::string();
    std::string response;

    curl_slist* headers = nullptr;
    headers = curl_slist_append(headers, "Content-Type: application/json");
    headers = curl_slist_append(headers, "Accept: application/json");

    const long timeout_ms = static_cast<long>(timeout_ * 1000.0);
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, collect_body);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, timeout_ms);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, timeout_ms);
    apply_auth(curl, username_, password_);

    if (body.has_value()) {
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, payload_text.c_str());
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(payload_text.size()));
        if (method != "POST") {
            curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, method.c_str());
        }
    } else if (method == "DELETE") {
        curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, "DELETE");
    } else {
        curl_easy_setopt(curl, CURLOPT_HTTPGET, 1L);
    }

    const CURLcode code = curl_easy_perform(curl);
    long status = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);

    if (code != CURLE_OK) {
        throw std::runtime_error(std::string("opencode request failed: ") +
                                 curl_easy_strerror(code));
    }

    if (status >= 400) {
        std::string message = response.substr(0, 400);
        std::optional<Json> parsed;
        try {
            Json value = Json::parse(response);
            if (value.is_object()) {
                const auto it_message = value.find("message");
                const auto it_error = value.find("error");
                if (it_message != value.end() && truthy(*it_message)) {
                    message = py_str(*it_message);
                } else if (it_error != value.end() && truthy(*it_error)) {
                    message = py_str(*it_error);
                }
            }
            parsed = std::move(value);
        } catch (const Json::exception&) {
            // A non-JSON error body keeps the truncated text as the message and
            // leaves the payload unset, as the Python original did.
        }
        throw HttpError(status, message, std::move(parsed));
    }

    if (response.empty()) {
        return Json(nullptr);
    }
    try {
        return Json::parse(response);
    } catch (const Json::exception&) {
        return Json(response);
    }
}

// ---------------------------------------------------------------- sessions

std::pair<std::vector<Json>, Json> OpenCodeClient::list_sessions(
    long long limit, const std::optional<std::string>& cursor,
    const std::optional<std::string>& directory, const std::optional<std::string>& search,
    const std::string& order) {
    Query query;
    query.emplace_back("limit", std::to_string(limit));
    if (cursor.has_value()) {
        query.emplace_back("cursor", *cursor);
    }
    if (directory.has_value()) {
        query.emplace_back("directory", *directory);
    }
    if (search.has_value()) {
        query.emplace_back("search", *search);
    }
    query.emplace_back("order", order);

    const Json data = request("GET", "/api/session", query, std::nullopt);

    // (data or {}).get("cursor") or {} -- a dictionary, not the session array.
    Json next_cursor = Json::object();
    if (data.is_object()) {
        const auto it = data.find("cursor");
        if (it != data.end() && truthy(*it)) {
            next_cursor = *it;
        }
    }
    return {list_from_data(data), std::move(next_cursor)};
}

Json OpenCodeClient::create_session(const std::optional<std::string>& title,
                                    const std::optional<std::string>& agent,
                                    const std::optional<Json>& model,
                                    const std::optional<std::string>& directory) {
    Json body = Json::object();
    if (present(title)) {
        body["title"] = *title;
    }
    if (present(agent)) {
        body["agent"] = *agent;
    }
    if (model.has_value() && truthy(*model)) {
        body["model"] = *model;
    }
    if (present(directory)) {
        body["location"] = Json{{"directory", *directory}};
    }
    return require_data(request("POST", "/api/session", {}, body));
}

Json OpenCodeClient::get_session(const std::string& session_id) {
    return require_data(request("GET", "/api/session/" + session_id, {}, std::nullopt));
}

std::optional<Json> OpenCodeClient::update_session(const std::string& session_id,
                                                   const std::optional<std::string>& title) {
    // `if title is not None`, so an empty title is still sent; an absent one
    // skips the request entirely.
    if (!title.has_value()) {
        return std::nullopt;
    }
    Json body = Json::object();
    body["title"] = *title;
    return optional_data(request("PATCH", "/api/session/" + session_id, {}, body));
}

void OpenCodeClient::delete_session(const std::string& session_id) {
    request("DELETE", "/api/session/" + session_id, {}, std::nullopt);
}

Json OpenCodeClient::fork_session(const std::string& session_id,
                                  const std::optional<std::string>& before) {
    Json body = Json::object();
    body["before"] = before.has_value() ? Json(*before) : Json(nullptr);
    return require_data(request("POST", "/api/session/" + session_id + "/fork", {}, body));
}

void OpenCodeClient::set_model(const std::string& session_id, const std::string& provider_id,
                               const std::string& model_id,
                               const std::optional<std::string>& variant) {
    Json ref = Json::object();
    ref["providerID"] = provider_id;
    ref["id"] = model_id;
    if (present(variant)) {
        ref["variant"] = *variant;
    }
    Json body = Json::object();
    body["model"] = ref;
    request("POST", "/api/session/" + session_id + "/model", {}, body);
}

void OpenCodeClient::set_agent(const std::string& session_id, const std::string& agent) {
    Json body = Json::object();
    body["agent"] = agent;
    request("POST", "/api/session/" + session_id + "/agent", {}, body);
}

// ---------------------------------------------------------------- messages

Json OpenCodeClient::prompt(const std::string& session_id, const std::string& text,
                            const std::optional<Json>& files,
                            const std::optional<std::string>& delivery) {
    Json body = Json::object();
    body["text"] = text;
    if (files.has_value() && truthy(*files)) {
        body["files"] = *files;
    }
    if (present(delivery)) {
        body["delivery"] = *delivery;
    }
    return require_data(request("POST", "/api/session/" + session_id + "/prompt", {}, body));
}

std::vector<Json> OpenCodeClient::messages(const std::string& session_id) {
    return list_from_data(
        request("GET", "/api/session/" + session_id + "/message", {}, std::nullopt));
}

Json OpenCodeClient::message(const std::string& session_id, const std::string& message_id) {
    return require_data(
        request("GET", "/api/session/" + session_id + "/message/" + message_id, {}, std::nullopt));
}

void OpenCodeClient::interrupt(const std::string& session_id) {
    request("POST", "/api/session/" + session_id + "/interrupt", {}, Json::object());
}

std::optional<Json> OpenCodeClient::compact(const std::string& session_id) {
    return optional_data(
        request("POST", "/api/session/" + session_id + "/compact", {}, Json::object()));
}

void OpenCodeClient::stage_revert(const std::string& session_id, const std::string& message_id) {
    Json body = Json::object();
    body["messageID"] = message_id;
    request("POST", "/api/session/" + session_id + "/revert/stage", {}, body);
}

void OpenCodeClient::clear_revert(const std::string& session_id) {
    request("DELETE", "/api/session/" + session_id + "/revert", {}, std::nullopt);
}

std::vector<Json> OpenCodeClient::permissions(const std::string& session_id) {
    return list_from_data(
        request("GET", "/api/session/" + session_id + "/permission", {}, std::nullopt));
}

std::optional<Json> OpenCodeClient::reply_permission(const std::string& session_id,
                                                     const std::string& request_id,
                                                     const std::string& decision,
                                                     const std::optional<std::string>& message) {
    Json body = Json::object();
    body["decision"] = decision;
    if (present(message)) {
        body["message"] = *message;
    }
    const Json result =
        request("POST", "/api/session/" + session_id + "/permission/" + request_id + "/reply", {},
                body);
    // The endpoint answers 204 with no body when it accepts the decision.
    return optional_data(result);
}

// ---------------------------------------------------------------------- forms

std::vector<Json> OpenCodeClient::forms(const std::string& session_id) {
    return list_from_data(
        request("GET", "/api/session/" + session_id + "/form", {}, std::nullopt));
}

Json OpenCodeClient::get_form(const std::string& session_id, const std::string& form_id) {
    return require_data(
        request("GET", "/api/session/" + session_id + "/form/" + form_id, {}, std::nullopt));
}

void OpenCodeClient::reply_form(const std::string& session_id, const std::string& form_id,
                                const Json& answer) {
    Json body = Json::object();
    body["answer"] = answer;
    request("POST", "/api/session/" + session_id + "/form/" + form_id + "/reply", {}, body);
}

void OpenCodeClient::cancel_form(const std::string& session_id, const std::string& form_id) {
    request("DELETE", "/api/session/" + session_id + "/form/" + form_id, {}, std::nullopt);
}

// ------------------------------------------------------------ config lookups

std::vector<Json> OpenCodeClient::models() {
    return list_from_data(request("GET", "/api/model", {}, std::nullopt));
}

std::vector<Json> OpenCodeClient::agents() {
    return list_from_data(request("GET", "/api/agent", {}, std::nullopt));
}

std::vector<std::pair<std::string, std::string>> OpenCodeClient::active_sessions() {
    const Json response = request("GET", "/api/session/active", {}, std::nullopt);
    std::vector<std::pair<std::string, std::string>> out;
    if (!response.is_object()) {
        return out;
    }
    const auto data = response.find("data");
    if (data == response.end() || !data->is_object()) {
        return out;
    }
    for (auto member = data->begin(); member != data->end(); ++member) {
        if (!member.value().is_object()) {
            continue;
        }
        const auto type = member.value().find("type");
        out.emplace_back(member.key(),
                         type != member.value().end() ? py_str(*type) : std::string());
    }
    return out;
}

Json OpenCodeClient::config() {
    const std::optional<Json> data = optional_data(request("GET", "/api/config", {}, std::nullopt));
    if (!data.has_value() || !truthy(*data)) {
        return Json::object();
    }
    return *data;
}

std::optional<Json> OpenCodeClient::default_model() {
    return optional_data(request("GET", "/api/model/default", {}, std::nullopt));
}

// -------------------------------------------------------------------- events

std::size_t OpenCodeClient::stream_write_callback(char* data, std::size_t size,
                                                  std::size_t nmemb, void* userdata) {
    auto* self = static_cast<OpenCodeClient*>(userdata);
    return self->on_stream_bytes(data, size * nmemb);
}

int OpenCodeClient::stream_progress_callback(void* userdata, curl_off_t, curl_off_t, curl_off_t,
                                             curl_off_t) {
    // Aborting here is what lets close() interrupt an in-flight transfer instead
    // of waiting out the low-speed timeout.
    auto* self = static_cast<OpenCodeClient*>(userdata);
    return self->closing_.load() ? 1 : 0;
}

std::size_t OpenCodeClient::on_stream_bytes(char* data, std::size_t length) {
    for (Json& event : parser_.feed(std::string_view(data, length))) {
        push_event(std::move(event));
    }
    parser_largest_.store(parser_.largest);
    parser_dropped_.store(parser_.dropped);
    return length;
}

void OpenCodeClient::push_event(Json event) {
    {
        const std::lock_guard<std::mutex> lock(mutex_);
        if (queue_.size() >= capacity_) {
            // Drop the oldest so a slow consumer cannot stall the stream.
            queue_.pop_front();
        }
        queue_.push_back(std::move(event));
    }
    cv_.notify_one();
}

void OpenCodeClient::stream_loop() {
    double backoff = 1.0;

    while (!closing_.load()) {
        ensure_curl();
        CURL* curl = curl_easy_init();
        if (curl == nullptr) {
            return;
        }

        const std::string url = base_ + "/api/event";
        curl_slist* headers = nullptr;
        headers = curl_slist_append(headers, "Accept: text/event-stream");

        curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, &OpenCodeClient::stream_write_callback);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, this);
        curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
        // No overall deadline: the stream is long lived. The low-speed settings
        // stand in for aiohttp's sock_read=90 on a dead connection.
        curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 30L);
        curl_easy_setopt(curl, CURLOPT_LOW_SPEED_LIMIT, 1L);
        curl_easy_setopt(curl, CURLOPT_LOW_SPEED_TIME, 90L);
        curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
        curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, &OpenCodeClient::stream_progress_callback);
        curl_easy_setopt(curl, CURLOPT_XFERINFODATA, this);
        apply_auth(curl, username_, password_);

        const CURLcode code = curl_easy_perform(curl);
        long status = 0;
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
        curl_slist_free_all(headers);
        curl_easy_cleanup(curl);

        if (closing_.load()) {
            break;
        }
        if (code == CURLE_OK && status == 200) {
            // A clean end of stream reconnects immediately, backoff reset.
            backoff = 1.0;
            continue;
        }

        reconnects_.fetch_add(1);
        {
            std::unique_lock<std::mutex> lock(mutex_);
            cv_.wait_for(lock, std::chrono::milliseconds(static_cast<long long>(backoff * 1000.0)),
                         [this] { return closing_.load(); });
        }
        backoff = std::min(backoff * 2.0, 30.0);
    }
}

std::string OpenCodeClient::start() {
    ensure_curl();
    closing_.store(false);

    std::string version;
    const Json info = request("GET", "/api/info", {}, std::nullopt);
    if (info.is_object()) {
        const auto it = info.find("version");
        if (it != info.end() && it->is_string()) {
            version = it->get<std::string>();
        }
    }

    if (!stream_thread_.joinable()) {
        stream_thread_ = std::thread(&OpenCodeClient::stream_loop, this);
    }
    return version;
}

void OpenCodeClient::close() {
    closing_.store(true);
    cv_.notify_all();
    if (stream_thread_.joinable()) {
        stream_thread_.join();
    }
    const std::lock_guard<std::mutex> lock(mutex_);
    queue_.clear();
}

std::vector<Json> OpenCodeClient::poll(std::size_t max_n, long long timeout_ms) {
    std::unique_lock<std::mutex> lock(mutex_);
    if (queue_.empty() && timeout_ms > 0 && !closing_.load()) {
        cv_.wait_for(lock, std::chrono::milliseconds(timeout_ms),
                     [this] { return !queue_.empty() || closing_.load(); });
    }

    std::vector<Json> out;
    while (!queue_.empty() && out.size() < max_n) {
        out.push_back(std::move(queue_.front()));
        queue_.pop_front();
    }
    return out;
}

}  // namespace engine

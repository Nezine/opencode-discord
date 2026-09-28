#pragma once

// HTTP client for the OpenCode API plus the long-lived event (SSE) stream.
//
// Port of bot/oc.py::OpenCodeClient. The engine owns the transport:
//   * request() is a blocking libcurl call; the binding releases the GIL around
//     it, so Python drives these from a worker thread.
//   * the event stream runs on its own thread and only ever touches C++ state.
//     Parsed events land in one bounded queue; Python drains them with poll()
//     and does the per-session fan-out, which is asyncio-shaped.

#include <atomic>
#include <condition_variable>
#include <cstddef>
#include <deque>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <curl/curl.h>

#include "json.hpp"
#include "sse.hpp"

namespace engine {

// An HTTP response with status >= 400. The binding turns this into the Python
// OpenCodeError, carrying status/message/payload.
class HttpError : public std::runtime_error {
public:
    HttpError(long status, std::string message, std::optional<Json> payload)
        : std::runtime_error(message), status_(status), message_(std::move(message)), payload_(std::move(payload)) {}

    long status() const { return status_; }
    const std::string& message() const { return message_; }
    const std::optional<Json>& payload() const { return payload_; }

private:
    long status_;
    std::string message_;
    std::optional<Json> payload_;
};

class OpenCodeClient {
public:
    OpenCodeClient(std::string url, std::string username, std::string password, double timeout);
    ~OpenCodeClient();

    OpenCodeClient(const OpenCodeClient&) = delete;
    OpenCodeClient& operator=(const OpenCodeClient&) = delete;

    const std::string& base() const { return base_; }

    // Probes /api/info and starts the event stream thread. Returns the server
    // version (empty when the response carried none).
    std::string start();
    void close();

    // Drains up to max_n queued events, waiting up to timeout_ms for the first.
    std::vector<Json> poll(std::size_t max_n, long long timeout_ms);

    std::size_t parser_largest() const { return parser_largest_.load(); }
    std::size_t parser_dropped() const { return parser_dropped_.load(); }
    long long reconnects() const { return reconnects_.load(); }

    // ------------------------------------------------------------- endpoints
    // Every one of these can throw HttpError.

    std::pair<std::vector<Json>, Json> list_sessions(long long limit,
                                                     const std::optional<std::string>& cursor,
                                                     const std::optional<std::string>& directory,
                                                     const std::optional<std::string>& search,
                                                     const std::string& order);
    Json create_session(const std::optional<std::string>& title,
                        const std::optional<std::string>& agent, const std::optional<Json>& model,
                        const std::optional<std::string>& directory);
    Json get_session(const std::string& session_id);
    std::optional<Json> update_session(const std::string& session_id,
                                       const std::optional<std::string>& title);
    void delete_session(const std::string& session_id);
    Json fork_session(const std::string& session_id, const std::optional<std::string>& before);
    void set_model(const std::string& session_id, const std::string& provider_id,
                   const std::string& model_id, const std::optional<std::string>& variant);
    void set_agent(const std::string& session_id, const std::string& agent);
    Json prompt(const std::string& session_id, const std::string& text,
                const std::optional<Json>& files, const std::optional<std::string>& delivery);
    std::vector<Json> messages(const std::string& session_id);
    Json message(const std::string& session_id, const std::string& message_id);
    void interrupt(const std::string& session_id);
    std::optional<Json> compact(const std::string& session_id);
    void stage_revert(const std::string& session_id, const std::string& message_id);
    void clear_revert(const std::string& session_id);
    std::vector<Json> permissions(const std::string& session_id);
    std::optional<Json> reply_permission(const std::string& session_id,
                                         const std::string& request_id,
                                         const std::string& decision,
                                         const std::optional<std::string>& message);
    std::vector<Json> forms(const std::string& session_id);
    Json get_form(const std::string& session_id, const std::string& form_id);
    void reply_form(const std::string& session_id, const std::string& form_id,
                    const Json& answer);
    void cancel_form(const std::string& session_id, const std::string& form_id);
    std::vector<Json> models();
    std::vector<Json> agents();
    std::vector<std::pair<std::string, std::string>> active_sessions();
    Json config();
    std::optional<Json> default_model();

private:
    using Query = std::vector<std::pair<std::string, std::string>>;

    Json request(const std::string& method, const std::string& path, const Query& query,
                 const std::optional<Json>& body);

    void stream_loop();
    std::size_t on_stream_bytes(char* data, std::size_t length);
    void push_event(Json event);

    static std::size_t stream_write_callback(char* data, std::size_t size, std::size_t nmemb,
                                             void* userdata);
    static int stream_progress_callback(void* userdata, curl_off_t dltotal, curl_off_t dlnow,
                                        curl_off_t ultotal, curl_off_t ulnow);

    std::string base_;
    std::string username_;
    std::string password_;
    double timeout_;

    std::thread stream_thread_;
    std::atomic<bool> closing_{false};
    std::atomic<long long> reconnects_{0};
    std::atomic<std::size_t> parser_largest_{0};
    std::atomic<std::size_t> parser_dropped_{0};

    std::mutex mutex_;
    std::condition_variable cv_;
    std::deque<Json> queue_;
    std::size_t capacity_ = 4000;

    // Touched only by the stream thread.
    SseParser parser_;
};

}  // namespace engine

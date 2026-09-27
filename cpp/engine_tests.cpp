// Standalone tests for the pure C++ engine logic.
//
// These run without Python so they can be built with AddressSanitizer and
// UBSan, which an instrumented extension module cannot easily be (it would
// clobber the in-tree .so and need LD_PRELOAD).
//
//   cmake -S . -B build-san -DENGINE_SANITIZE=ON && cmake --build build-san
//   ./build-san/engine_tests

#include <algorithm>
#include <cstdio>
#include <string>
#include <vector>

#include "sse.hpp"
#include "store.hpp"
#include "text.hpp"
#include "turn.hpp"
#include "utf8.hpp"

namespace {

using Json = engine::Json;

int failures = 0;

void check(const std::string& name, bool ok, const std::string& detail = "") {
    if (ok) {
        std::printf("  ok   %s\n", name.c_str());
    } else {
        std::printf("  FAIL %s %s\n", name.c_str(), detail.c_str());
        ++failures;
    }
}

template <typename T>
void eq(const std::string& name, const T& actual, const T& expected) {
    check(name, actual == expected, "\n       got:      " + std::to_string(actual) +
                                        "\n       expected: " + std::to_string(expected));
}

void eq(const std::string& name, const std::string& actual, const std::string& expected) {
    check(name, actual == expected,
          "\n       got:      " + actual + "\n       expected: " + expected);
}

void test_text() {
    std::printf("\ntext\n");

    eq("clip leaves short text", text::clip("hello", 1900), std::string("hello"));
    eq("clip truncates with ellipsis", text::clip("abcdefghij", 8), std::string("abcde..."));
    // 'a' + 4-byte emoji: 3 code points, well under any byte-based mistake.
    eq("clip counts code points", text::clip("a\xF0\x9F\x98\x80\xF0\x9F\x98\x80", 3),
       std::string("a\xF0\x9F\x98\x80\xF0\x9F\x98\x80"));
    eq("clip trims trailing space", text::clip("abc   defghij", 8), std::string("abc..."));
    eq("clip of empty", text::clip("", 10), std::string(""));

    auto one = text::split_text("hello", 1900);
    eq("short text is one chunk", one.size(), std::size_t{1});
    if (one.size() == 1) {
        eq("short chunk value", one[0], std::string("hello"));
    }

    eq("empty splits to nothing", text::split_text("", 1900).size(), std::size_t{0});

    // A fenced block split across chunks must stay balanced.
    const std::string fenced =
        "```python\n" + std::string(120, 'x') + "\n" + std::string(120, 'y') + "\n```";
    const auto chunks = text::split_text(fenced, 80);
    check("fence split produced chunks", chunks.size() > 1);
    for (const auto& chunk : chunks) {
        const auto ticks = std::count(chunk.begin(), chunk.end(), '`');
        check("chunk keeps fence balance", ticks % 3 == 0, "\n       " + chunk);
    }

    eq("tilde fence is honoured",
       text::split_text("~~~\n" + std::string(200, 'z') + "\n~~~", 100).size() > 1, true);

    eq("mentions are defanged", text::sanitize_mentions("@everyone @here"),
       std::string("@\u200beveryone @\u200bhere"));
    eq("mentions of empty", text::sanitize_mentions(""), std::string(""));

    eq("rel_time of absent", text::rel_time(std::nullopt), std::string("unknown"));
    eq("rel_time of zero", text::rel_time(0.0), std::string("unknown"));
    // `ts` is milliseconds; `now` is seconds.
    eq("rel_time just now", text::rel_time(1000000.0, 1001.0), std::string("just now"));
    eq("rel_time minutes", text::rel_time(300000.0, 600.0), std::string("5m ago"));
    eq("rel_time hours", text::rel_time(3600000.0, 10800.0), std::string("2h ago"));
    eq("rel_time days", text::rel_time(86400000.0, 345600.0), std::string("3d ago"));
    // Past 30 days it falls back to a local date, so only assert the shape.
    const std::string dated = text::rel_time(86400000.0, 86400.0 + 3000000.0);
    eq("rel_time date length", dated.size(), std::size_t{10});
    eq("rel_time date shape", dated[4] == '-' && dated[7] == '-', true);

    eq("duration sub-second", text::duration(0.25), std::string("250ms"));
    eq("duration seconds", text::duration(12.34), std::string("12.3s"));
    eq("duration minutes", text::duration(125.0), std::string("2m 5s"));

    eq("human_cost of zero", text::human_cost(0.0), std::string("$0.00"));
    eq("human_cost of absent", text::human_cost(std::nullopt), std::string("$0.00"));
    eq("human_cost small", text::human_cost(0.0012), std::string("$0.0012"));
    eq("human_cost normal", text::human_cost(1.5), std::string("$1.50"));

    eq("format_tokens plain", text::format_tokens(999), std::string("999 tokens"));
    eq("format_tokens thousands", text::format_tokens(1500), std::string("1.5k tokens"));
    eq("format_tokens millions", text::format_tokens(2500000), std::string("2.5M tokens"));
}

void test_sse() {
    std::printf("\nsse\n");

    engine::SseParser parser;
    const auto events = parser.feed("data: {\"type\":\"a\"}\n");
    eq("single event parsed", events.size(), std::size_t{1});
    if (events.size() == 1) {
        eq("event type", events[0].value("type", ""), std::string("a"));
    }

    engine::SseParser named;
    const auto injected = named.feed("event: custom\ndata: {\"foo\":1}\n");
    eq("event name is injected", injected.size(), std::size_t{1});
    if (injected.size() == 1) {
        eq("injected type value", injected[0].value("type", ""), std::string("custom"));
        eq("payload preserved", injected[0].value("foo", 0), 1);
    }

    engine::SseParser done;
    eq("[DONE] is ignored", done.feed("data: [DONE]\n").size(), std::size_t{0});

    engine::SseParser crlf;
    eq("CRLF handled", crlf.feed("data: {\"a\":1}\r\n").size(), std::size_t{1});

    engine::SseParser scalar;
    eq("non-dict payload ignored", scalar.feed("data: [1,2,3]\n").size(), std::size_t{0});

    engine::SseParser garbage;
    eq("unparseable payload ignored", garbage.feed("data: not json\n").size(), std::size_t{0});

    engine::SseParser partial;
    eq("partial line buffers", partial.feed("data: {\"a\"").size(), std::size_t{0});
    eq("buffered line completes", partial.feed(":1}\n").size(), std::size_t{1});

    engine::SseParser capped;
    capped.max_line = 1024;
    eq("oversized line dropped", capped.feed("data: {\"pad\":\"" + std::string(5000, 'z')).size(),
       std::size_t{0});
    eq("drop counted", capped.dropped, std::size_t{1});
    eq("parser still usable", capped.feed("data: {\"type\":\"after\"}\n").size(), std::size_t{1});

    engine::SseParser largest;
    largest.feed("data: {\"type\":\"x\"}\n");
    eq("largest is tracked", largest.largest > 0, true);
}

void test_store() {
    std::printf("\nstore\n");

    using Field = std::pair<std::string, std::optional<std::string>>;

    engine::Store store(":memory:");

    const engine::UserState empty = store.get_user(42);
    eq("unknown user has no session", empty.session_id.has_value(), false);
    eq("no model yet", empty.has_model(), false);
    eq("no model label", empty.model_label(), std::string("default"));

    engine::UserState state;
    state.user_id = 42;
    state.session_id = "ses_a";
    state.provider_id = "google";
    state.model_id = "gemini";
    state.variant = "high";
    state.agent = "build";
    state.directory = "/tmp";
    state.title = "first";
    store.save_user(state);

    const engine::UserState loaded = store.get_user(42);
    eq("session round trips", loaded.session_id.value_or(""), std::string("ses_a"));
    eq("model round trips", loaded.model_label(), std::string("google/gemini \u00b7 high"));
    eq("directory round trips", loaded.directory.value_or(""), std::string("/tmp"));

    const engine::UserState updated =
        store.update(42, {Field{"session_id", "ses_b"}, Field{"title", "second"},
                          Field{"bogus_field", "ignored"}});
    eq("update applies", updated.session_id.value_or(""), std::string("ses_b"));
    eq("update keeps other fields", updated.title.value_or(""), std::string("second"));
    eq("update persists", store.get_user(42).session_id.value_or(""), std::string("ses_b"));
    eq("other user untouched", store.get_user(43).session_id.has_value(), false);

    store.remember(42, "ses_a", "first");
    store.remember(42, "ses_b", "second");
    const auto ids = store.conversation_ids(42);
    eq("both conversations tracked", ids.size(), std::size_t{2});
    eq("most recent first", ids.empty() ? std::string() : ids[0], std::string("ses_b"));
    store.forget(42, "ses_a");
    eq("forget removes one", store.conversation_ids(42).size(), std::size_t{1});
    store.forget(42, "ses_b");
    eq("forget removes the rest", store.conversation_ids(42).size(), std::size_t{0});

    // Nullable titles survive a round trip.
    store.remember(42, "ses_c", std::nullopt);
    eq("null title accepted", store.conversation_ids(42).size(), std::size_t{1});

    // model_label / has_model edge cases.
    engine::UserState edge;
    edge.model_id = "";
    eq("empty model id labels default", edge.model_label(), std::string("default"));
    edge.model_id = "gemini";
    edge.provider_id = "";
    eq("empty provider is not has_model", edge.has_model(), false);
    edge.variant = "default";
    eq("default variant is omitted", edge.model_label(), std::string("/gemini"));
    edge.provider_id = "google";
    edge.variant = "low";
    eq("variant is appended", edge.model_label(), std::string("google/gemini \u00b7 low"));
    eq("has_model with both set", edge.has_model(), true);
}

void test_turn() {
    std::printf("\nturn\n");

    const auto ev = [](const std::string& kind, Json data) {
        Json event = Json::object();
        event["type"] = kind;
        data["sessionID"] = "ses_test";
        event["data"] = std::move(data);
        return event;
    };

    // tool_detail: the key fallback iterates dict order, so an ordered object
    // type is required or the keys come back sorted.
    eq("tool_detail command", engine::tool_detail(Json{{"command", "ls"}}), std::string("ls"));
    // 41 code points (width - 1) plus the ellipsis, measured in code points as
    // Python's len() would.
    eq("tool_detail clipped",
       utf8::decode(engine::tool_detail(Json{{"path", std::string(200, 'x')}})).size(),
       std::size_t{42});
    eq("tool_detail key order", engine::tool_detail(Json{{"weird", 1}, {"other", 2}}),
       std::string("weird,other"));
    eq("tool_detail empty", engine::tool_detail(Json::object()), std::string(""));

    eq("token_total sums",
       engine::token_total(
           Json{{"input", 10}, {"output", 5}, {"reasoning", 1}, {"cache", {{"read", 2}, {"write", 3}}}}),
       21LL);
    eq("token_total of empty", engine::token_total(Json::object()), 0LL);

    engine::TurnState state("ses_test");
    engine::apply_event(state, ev("session.step.started", {{"assistantMessageID", "msg_1"}}));
    engine::apply_event(state,
                        ev("session.text.started", {{"assistantMessageID", "msg_1"}, {"ordinal", 0}}));
    engine::apply_event(state, ev("session.text.delta",
                                  {{"assistantMessageID", "msg_1"}, {"ordinal", 0}, {"delta", "Hel"}}));
    engine::apply_event(state, ev("session.text.delta",
                                  {{"assistantMessageID", "msg_1"}, {"ordinal", 0}, {"delta", "lo"}}));
    eq("deltas accumulate in order", state.text(), std::string("Hello"));
    check("status mentions thinking",
          engine::render_status(state, "m/x").find("thinking") != std::string::npos);
    check("status mentions the model",
          engine::render_status(state, "m/x").find("m/x") != std::string::npos);

    engine::apply_event(state, ev("session.tool.called", {{"assistantMessageID", "msg_1"},
                                                          {"id", "call_1"},
                                                          {"input", {{"command", "ls -la"}}}}));
    eq("one step tracked", state.steps.size(), std::size_t{1});
    const auto step = state.steps.front();
    const auto tool = step->tool("call_1");
    check("tool was created", tool != nullptr);
    eq("tool starts running", tool->status, std::string("running"));
    eq("tool detail from input", tool->detail, std::string("ls -la"));
    check("tool line rendered", engine::render_tools(*step).find("tool") != std::string::npos);

    {
        Json message = Json::object();
        message["id"] = "msg_1";
        message["content"] = Json::array({
            Json{{"type", "tool"},
                 {"id", "call_1"},
                 {"name", "bash"},
                 {"state", {{"status", "completed"}, {"input", {{"command", "ls"}}}}}},
            Json{{"type", "reasoning"}, {"text", "I should list files"}},
        });
        engine::hydrate_from_message(state, message);
    }
    // The handles captured above must observe the hydration.
    eq("tool name resolved", tool->name, std::string("bash"));
    eq("tool status resolved", tool->status, std::string("completed"));
    eq("reasoning hydrated", step->thinking(), std::string("I should list files"));
    check("reasoning rendered",
          engine::render_reasoning(step->thinking()).find("thinking") != std::string::npos);

    engine::apply_event(state, ev("session.tool.failed",
                                  {{"assistantMessageID", "msg_1"},
                                   {"id", "call_2"},
                                   {"error", {{"type", "tool.execution"}, {"message", "boom"}}}}));
    const auto failed = step->tool("call_2");
    check("failure recorded", failed != nullptr);
    eq("failure status", failed->status, std::string("error"));
    eq("failure message", failed->error, std::string("boom"));

    engine::apply_event(state, ev("session.step.ended",
                                  {{"assistantMessageID", "msg_1"},
                                   {"finish", "stop"},
                                   {"cost", 0.5},
                                   {"tokens",
                                    {{"input", 10},
                                     {"output", 5},
                                     {"reasoning", 0},
                                     {"cache", {{"read", 0}, {"write", 0}}}}}}));
    check("step finished", step->finished);
    eq("cost recorded", state.cost, 0.5);

    // A second step becomes its own Step.
    engine::apply_event(state, ev("session.step.started", {{"assistantMessageID", "msg_2"}}));
    engine::apply_event(state, ev("session.text.delta",
                                  {{"assistantMessageID", "msg_2"}, {"ordinal", 0}, {"delta", "second"}}));
    eq("two steps tracked", state.steps.size(), std::size_t{2});
    eq("first step text intact", state.steps.front()->text(), std::string("Hello"));
    eq("second step text", state.steps.back()->text(), std::string("second"));
    eq("turn text joins steps", state.text(), std::string("Hello\n\nsecond"));

    engine::apply_event(state, ev("session.execution.succeeded", Json::object()));
    eq("outcome recorded", state.outcome.value_or(""), std::string("succeeded"));

    // Failures.
    engine::TurnState failing("ses_test");
    engine::apply_event(failing, ev("session.step.started", {{"assistantMessageID", "msg_1"}}));
    engine::apply_event(failing,
                        ev("session.step.failed", {{"assistantMessageID", "msg_1"},
                                                   {"error", {{"type", "aborted"}, {"message", "Step interrupted"}}}}));
    engine::apply_event(failing, ev("session.execution.failed",
                                    {{"error", {{"type", "aborted"}, {"message", "aborted by user"}}}}));
    eq("outcome failed", failing.outcome.value_or(""), std::string("failed"));
    eq("error message kept", failing.error.value_or(""), std::string("aborted by user"));
    eq("step error kept", failing.steps.front()->error.value_or(""), std::string("Step interrupted"));

    // The execution-level error falls back to the type field, unlike step errors.
    engine::TurnState typed("ses_test");
    engine::apply_event(typed, ev("session.execution.failed", {{"error", {{"type", "aborted"}}}}));
    eq("error falls back to type", typed.error.value_or(""), std::string("aborted"));

    engine::TurnState retry("ses_test");
    engine::apply_event(retry, ev("session.step.started", {{"assistantMessageID", "msg_1"}}));
    engine::apply_event(retry, ev("session.retry.scheduled", Json::object()));
    check("retry surfaced", engine::render_status(retry).find("retry") != std::string::npos);

    engine::TurnState perms("ses_test");
    engine::apply_event(perms, ev("permission.asked", {{"id", "req_1"}}));
    eq("permission pending", perms.pending_permissions(), std::size_t{1});
    engine::apply_event(perms, ev("permission.replied", {{"requestID", "req_1"}}));
    eq("permission cleared", perms.pending_permissions(), std::size_t{0});

    // render_footer drops the zero-cost / zero-token placeholders.
    engine::TurnState footer("ses_test");
    engine::apply_event(footer, ev("session.step.started", {{"assistantMessageID", "m"}}));
    const auto fstep = footer.steps.front();
    const std::string bare = engine::render_footer(footer, *fstep, "m/x");
    check("footer starts with the model", bare.rfind("m/x", 0) == 0);
    check("footer drops the cost placeholder", bare.find("$0.00") == std::string::npos);
    check("footer drops the token placeholder", bare.find("0 tokens") == std::string::npos);
    fstep->cost = 1.5;
    check("footer includes cost",
          engine::render_footer(footer, *fstep, "m/x").find("$1.50") != std::string::npos);
}

}  // namespace

int main() {
    test_text();
    test_sse();
    test_store();
    test_turn();

    std::printf("\n%s\n", std::string(40, '=').c_str());
    if (failures != 0) {
        std::printf("%d FAILED\n", failures);
        return 1;
    }
    std::printf("all engine tests passed\n");
    return 0;
}

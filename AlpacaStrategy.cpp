#pragma once

#include <string>
#include <string_view>
#include <optional>
#include <chrono>
#include <stdexcept>
#include <sstream>
#include <fstream>
#include <StrategyTemplate.hpp>       // Common types and TradingApiBase
#include <string_view>

#include <toml++/toml.h>   // toml++ (header-only)
#include <curl/curl.h>     // libcurl

struct AlpacaConfig {
    std::string api_key;
    std::string api_secret;
    std::string base_url;   // paper: https://paper-api.alpaca.markets
};

class AlpacaPaperApi : public StrategyTemplate<AlpacaPaperApi> {
public:
    explicit AlpacaPaperApi(const std::string& tomlPath)
        : cfg_(load_config(tomlPath))
    {
        curl_global_init(CURL_GLOBAL_DEFAULT);
    }

    ~AlpacaPaperApi() {
        curl_global_cleanup();
    }

    // ===== required *_Impl methods =====

    OrderResult PlaceMarketOrder_Impl(const MarketOrder& o) {
        // POST /v2/orders
        const std::string body = build_order_json(
            o.symbol, (o.side == Side::Buy ? "buy" : "sell"),
            o.qty, "market", /*limit*/std::nullopt, /*stop*/std::nullopt, to_tif(o.tif)
        );
        auto [status, resp] = http_post(cfg_.base_url + "/v2/orders", body);
        return parse_order_result(status, resp);
    }

    OrderResult PlaceShortOrder_Impl(const ShortOrder& o) {
        // In Alpaca: short is typically sell on a non-owned position (subject to margin/shortability).
        const std::string body = build_order_json(
            o.symbol, "sell", o.qty, "market", std::nullopt, std::nullopt, to_tif(o.tif)
        );
        auto [status, resp] = http_post(cfg_.base_url + "/v2/orders", body);
        return parse_order_result(status, resp);
    }

    OrderResult PlaceStopOrder_Impl(const StopOrder& o) {
        // Alpaca stop order: type="stop", stop_price
        const std::string body = build_order_json(
            o.symbol, (o.side == Side::Buy ? "buy" : "sell"),
            o.qty, "stop", std::nullopt, o.stopPrice, to_tif(o.tif)
        );
        auto [status, resp] = http_post(cfg_.base_url + "/v2/orders", body);
        return parse_order_result(status, resp);
    }

    OrderResult PlaceLimitOrder_Impl(const LimitOrder& o) {
        const std::string body = build_order_json(
            o.symbol, (o.side == Side::Buy ? "buy" : "sell"),
            o.qty, "limit", o.limitPrice, std::nullopt, to_tif(o.tif)
        );
        auto [status, resp] = http_post(cfg_.base_url + "/v2/orders", body);
        return parse_order_result(status, resp);
    }

    PositionCloseResult CloseAllPositions_Impl() {
        // DELETE /v2/positions
        auto [status, resp] = http_delete(cfg_.base_url + "/v2/positions");
        if (status >= 200 && status < 300) return { true, "Closed all positions." };
        return { false, "CloseAllPositions failed: HTTP " + std::to_string(status) + " " + resp };
    }

    std::optional<SysTime> GetNextMarketOpenTime_Impl() {
        auto [status, resp] = http_get(cfg_.base_url + "/v2/clock");
        if (status < 200 || status >= 300) return std::nullopt;

        // Make the search token a string_view (works with std::string::find(string_view) in C++17+)
        constexpr std::string_view key = "\"next_open\":\"";

        auto pos = resp.find(key);
        if (pos == std::string::npos) return std::nullopt;
        pos += key.size();

        auto end = resp.find('"', pos);
        if (end == std::string::npos) return std::nullopt;

        std::string iso = resp.substr(pos, end - pos);
        // TODO: parse iso -> SysTime
        return SysTime{}; // placeholder
    }

private:
    AlpacaConfig cfg_;

    // ---------- TOML ----------
    static AlpacaConfig load_config(const std::string& tomlPath) {
        toml::table tbl = toml::parse_file(tomlPath);

        auto get_str = [&](std::string_view dotted) -> std::string {
            auto v = tbl.at_path(dotted);
            if (!v || !v.is_string()) throw std::runtime_error("Missing/invalid TOML key: " + std::string(dotted));
            return *v.value<std::string>();
        };

        AlpacaConfig cfg;
        cfg.api_key    = get_str("alpaca.api_key");
        cfg.api_secret = get_str("alpaca.api_secret");
        cfg.base_url   = get_str("alpaca.base_url");

        if (cfg.api_key.empty() || cfg.api_secret.empty() || cfg.base_url.empty())
            throw std::runtime_error("Invalid Alpaca config (empty fields).");

        return cfg;
    }

    // ---------- HTTP ----------
    static size_t curl_write_cb(void* contents, size_t size, size_t nmemb, void* userp) {
        const size_t total = size * nmemb;
        auto* s = static_cast<std::string*>(userp);
        s->append(static_cast<char*>(contents), total);
        return total;
    }

    struct HttpResp { long status; std::string body; };

    HttpResp http_req(std::string_view method, const std::string& url, const std::optional<std::string>& body = std::nullopt) {
        CURL* curl = curl_easy_init();
        if (!curl) throw std::runtime_error("curl_easy_init failed");

        std::string response;
        curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, std::string(method).c_str());
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, &curl_write_cb);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);

        // headers
        struct curl_slist* headers = nullptr;
        headers = curl_slist_append(headers, "Content-Type: application/json");
        headers = curl_slist_append(headers, ("APCA-API-KEY-ID: " + cfg_.api_key).c_str());
        headers = curl_slist_append(headers, ("APCA-API-SECRET-KEY: " + cfg_.api_secret).c_str());
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);

        if (body.has_value()) {
            curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body->c_str());
        }

        CURLcode res = curl_easy_perform(curl);
        long status = 0;
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);

        curl_slist_free_all(headers);
        curl_easy_cleanup(curl);

        if (res != CURLE_OK) {
            throw std::runtime_error(std::string("curl failed: ") + curl_easy_strerror(res));
        }
        return { status, response };
    }

    std::pair<long, std::string> http_get(const std::string& url)    { auto r = http_req("GET", url); return {r.status, r.body}; }
    std::pair<long, std::string> http_delete(const std::string& url) { auto r = http_req("DELETE", url); return {r.status, r.body}; }
    std::pair<long, std::string> http_post(const std::string& url, const std::string& body) { auto r = http_req("POST", url, body); return {r.status, r.body}; }

    // ---------- Helpers ----------
    static std::string to_tif(TimeInForce tif) {
        switch (tif) {
            case TimeInForce::Day: return "day";
            case TimeInForce::GTC: return "gtc";
            case TimeInForce::IOC: return "ioc";
            case TimeInForce::FOK: return "fok";
        }
        return "day";
    }

    static std::string build_order_json(
        const std::string& symbol,
        const std::string& side,
        double qty,
        const std::string& type,
        std::optional<double> limit_price,
        std::optional<double> stop_price,
        const std::string& tif
    ) {
        // Minimal JSON builder (good enough; swap to nlohmann/json later if you want)
        std::ostringstream ss;
        ss << "{";
        ss << "\"symbol\":\"" << symbol << "\",";
        ss << "\"qty\":\"" << qty << "\",";          // Alpaca accepts qty as string in examples
        ss << "\"side\":\"" << side << "\",";
        ss << "\"type\":\"" << type << "\",";
        ss << "\"time_in_force\":\"" << tif << "\"";
        if (limit_price) ss << ",\"limit_price\":\"" << *limit_price << "\"";
        if (stop_price)  ss << ",\"stop_price\":\"" << *stop_price  << "\"";
        ss << "}";
        return ss.str();
    }

    static OrderResult parse_order_result(long status, const std::string& resp) {
        if (status >= 200 && status < 300) {
            // Minimal parse: try to find "id":"..."
            std::string id;
            const std::string key = "\"id\":\"";
            auto pos = resp.find(key);
            if (pos != std::string::npos) {
                pos += key.size();
                auto end = resp.find('"', pos);
                if (end != std::string::npos) id = resp.substr(pos, end - pos);
            }
            return { id, true, "Accepted" };
        }
        return { "", false, "Order failed: HTTP " + std::to_string(status) + " " + resp };
    }
};
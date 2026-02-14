#include <thread>
#include <string>
#include <optional>
#include <iostream>
#include <sstream>
#include <iomanip>
#include <chrono>

using SysTime = std::chrono::system_clock::time_point;

static std::string to_iso8601_utc(SysTime tp) {
    std::time_t t = std::chrono::system_clock::to_time_t(tp);
    std::tm gmt{};
#if defined(_WIN32)
    gmtime_s(&gmt, &t);
#else
    gmt = *std::gmtime(&t);
#endif
    std::ostringstream oss;
    oss << std::put_time(&gmt, "%Y-%m-%dT%H:%M:%SZ");
    return oss.str();
}

static SysTime floor_to_day_utc(SysTime tp) {
    using namespace std::chrono;
    auto s = time_point_cast<seconds>(tp);
    auto days = floor<std::chrono::days>(s); // C++20
    return SysTime(days);
}

// Select a market (can be expanded to other markets later)

enum class Market { US, CN, NONE };

struct MarketPick {
    Market market{Market::NONE};
    std::string symbol; // index proxy symbol
};



// ---------- “First 5-min candle of current trading day” ----------
//
// US: use Alpaca clock to get session open, then query 5Min bars from that open time, limit 1.
// CN: placeholder (implement later).

struct Candle {
    std::string t;    // timestamp (ISO string for now)
    double o=0, h=0, l=0, c=0;
    long v=0;
    bool ok=false;
    std::string raw;  // raw JSON (until you add a JSON parser)
};

// TODO: This should live in your Alpaca API class. Shown standalone for clarity.
// You need a Data API base URL (NOT paper trading). Typically: https://data.alpaca.markets
// Endpoint: GET /v2/stocks/bars?symbols=SPY&timeframe=5Min&start=...&limit=1
//
// For now we return raw JSON and do minimal parsing later.
template <class AlpacaApi>
static Candle GetFirst5MinCandle_US(AlpacaApi& alpaca, const std::string& symbol) {
    // 1) Get /v2/clock from trading base URL (paper-api), parse "next_open" and "timestamp" if you want.
    // But best: parse "next_open" and "timestamp" and also "is_open".
    // Here we’ll do a minimal approach:
    auto [stClock, clockJson] = alpaca.http_get(alpaca.trading_base_url() + "/v2/clock");
    if (stClock < 200 || stClock >= 300) {
        return Candle{.ok=false, .raw="clock failed: " + clockJson};
    }

    // Minimal string extraction for "timestamp" and either "next_open" or "open" is annoying.
    // Better approach:
    // - If market is open: first bar start = today's session open (needs calendar)
    // - Easiest reliable approach: call Alpaca calendar endpoint for today and get session_open.
    //
    // Alpaca trading: GET /v2/calendar?start=YYYY-MM-DD&end=YYYY-MM-DD
    // We'll do that instead.

    // Extract today's UTC date as YYYY-MM-DD
    auto nowUtc = std::chrono::system_clock::now();
    std::time_t t = std::chrono::system_clock::to_time_t(nowUtc);
    std::tm gmt{};
#if defined(_WIN32)
    gmtime_s(&gmt, &t);
#else
    gmt = *std::gmtime(&t);
#endif
    char dateBuf[11];
    std::snprintf(dateBuf, sizeof(dateBuf), "%04d-%02d-%02d", gmt.tm_year + 1900, gmt.tm_mon + 1, gmt.tm_mday);
    std::string ymd(dateBuf);

    // 2) Trading calendar for today
    auto calUrl = alpaca.trading_base_url() + "/v2/calendar?start=" + ymd + "&end=" + ymd;
    auto [stCal, calJson] = alpaca.http_get(calUrl);
    if (stCal < 200 || stCal >= 300) {
        return Candle{.ok=false, .raw="calendar failed: " + calJson};
    }

    // Minimal parse: look for "open":"09:30" and build UTC start via Alpaca data API
    // NOTE: calendar times are in America/New_York local time. Converting correctly needs timezone handling.
    //
    // Practical shortcut (robust): query 5Min bars for the day with start=ymdT00:00Z and take the first bar returned.
    // That works because Alpaca returns only trading bars anyway.
    // We'll do that.

    std::string start = ymd + "T00:00:00Z";
    auto url = alpaca.data_base_url() +
        "/v2/stocks/bars?symbols=" + symbol +
        "&timeframe=5Min&start=" + start +
        "&limit=1";

    auto [stBars, barsJson] = alpaca.http_get(url);
    Candle out;
    out.raw = barsJson;
    out.ok = (stBars >= 200 && stBars < 300);
    return out;
}

static Candle GetFirst5MinCandle_CN_Placeholder(const std::string& /*symbol*/) {
    return Candle{.ok=false, .raw="CN market data not implemented yet"};
}

// ---------- Strategy chooser ----------

template <class AlpacaApi>
static MarketPick pick_market(AlpacaApi& alpaca) {
    // Prefer US if open (uses Alpaca clock => DST handled)
    if (alpaca.IsMarketOpen()) {
        return { Market::US, "SPY" }; // S&P proxy; you can choose VOO/IVV as well
    }

    // Otherwise check CN by UTC window (placeholder until you implement a real CN calendar)
    auto nowUtc = std::chrono::system_clock::now();
    if (is_cn_market_open_utc(nowUtc)) {
        return { Market::CN, "SSE" }; // placeholder symbol key; your CN impl will map it
    }

    return { Market::NONE, "" };
}

// ---------- Live loop ----------

template <class AlpacaApi>
void live_loop(AlpacaApi& alpaca) {
    using namespace std::chrono;

    while (true) {
        auto nowUtc = system_clock::now();

        auto pick = pick_market(alpaca);

        if (pick.market == Market::US) {
            auto c = GetFirst5MinCandle_US(alpaca, pick.symbol);
            std::cout << "[UTC " << to_iso8601_utc(nowUtc) << "] US open. First 5m candle (" << pick.symbol << "): "
                      << (c.ok ? "OK" : "FAIL") << "\n"
                      << c.raw << "\n";
        }
        else if (pick.market == Market::CN) {
            auto c = GetFirst5MinCandle_CN_Placeholder(pick.symbol);
            std::cout << "[UTC " << to_iso8601_utc(nowUtc) << "] CN open. First 5m candle (" << pick.symbol << "): "
                      << (c.ok ? "OK" : "FAIL") << "\n"
                      << c.raw << "\n";
        }
        else {
            std::cout << "[UTC " << to_iso8601_utc(nowUtc) << "] No tracked market open.\n";
        }

        // Sleep a bit (don’t spam APIs)
        std::this_thread::sleep_for(std::chrono::seconds(30));
    }
}

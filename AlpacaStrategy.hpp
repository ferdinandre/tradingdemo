// AlpacaStrategy.hpp
#pragma once

#include <string>
#include <optional>
#include <chrono>

#include "StrategyTemplate.hpp"

struct OrderResult;
struct PositionCloseResult;
struct MarketOrder;
struct LimitOrder;
struct StopOrder;
struct ShortOrder;

using SysTime = std::chrono::system_clock::time_point;

class AlpacaStrategy : public StrategyTemplate<AlpacaStrategy> {
public:
    explicit AlpacaStrategy(const std::string& tomlPath);
    ~AlpacaStrategy();

    // ===== Required by TradingApiBase (CRTP contract) =====
    OrderResult PlaceMarketOrder_Impl(const MarketOrder& o);
    OrderResult PlaceShortOrder_Impl(const ShortOrder& o);
    OrderResult PlaceStopOrder_Impl(const StopOrder& o);
    OrderResult PlaceLimitOrder_Impl(const LimitOrder& o);

    PositionCloseResult CloseAllPositions_Impl();

    bool IsMarketOpen_Impl();
    std::optional<SysTime> GetNextMarketOpenTime_Impl();

    // Optional helpers
    const std::string& trading_base_url() const;
    const std::string& data_base_url() const;

private:
        struct AlpacaConfig {
        std::string api_key;
        std::string api_secret;
        std::string base_url;   // paper: https://paper-api.alpaca.markets
    };
    AlpacaConfig cfg_;
};
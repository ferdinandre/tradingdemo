#pragma once
#include <string>
#include <string_view>
#include <optional>
#include <chrono>
#include <stdexcept>

using SysTime = std::chrono::system_clock::time_point;

enum class Side { Buy, Sell };
enum class TimeInForce { Day, GTC, IOC, FOK };

struct OrderResult {
    std::string orderId;
    bool accepted = false;
    std::string message;
};

struct PositionCloseResult {
    bool success = false;
    std::string message;
};

struct MarketOrder {
    std::string symbol;
    Side side;
    double qty;
    TimeInForce tif{TimeInForce::Day};
};

struct LimitOrder {
    std::string symbol;
    Side side;
    double qty;
    double limitPrice;
    TimeInForce tif{TimeInForce::Day};
};

struct StopOrder {
    std::string symbol;
    Side side;
    double qty;
    double stopPrice;
    TimeInForce tif{TimeInForce::Day};
};

struct ShortOrder {
    std::string symbol;
    double qty;
    TimeInForce tif{TimeInForce::Day};
};

template <class Derived>
class StrategyTemplate {
public:
    OrderResult PlaceMarketOrder(const MarketOrder& o) {
        validate_basic(o.symbol, o.qty);
        return derived().PlaceMarketOrder_Impl(o);
    }

    OrderResult PlaceShortOrder(const ShortOrder& o) {
        validate_basic(o.symbol, o.qty);
        return derived().PlaceShortOrder_Impl(o);
    }

    OrderResult PlaceStopOrder(const StopOrder& o) {
        validate_basic(o.symbol, o.qty);
        if (o.stopPrice <= 0.0) throw std::invalid_argument("stopPrice must be > 0");
        return derived().PlaceStopOrder_Impl(o);
    }

    OrderResult PlaceLimitOrder(const LimitOrder& o) {
        validate_basic(o.symbol, o.qty);
        if (o.limitPrice <= 0.0) throw std::invalid_argument("limitPrice must be > 0");
        return derived().PlaceLimitOrder_Impl(o);
    }

    PositionCloseResult CloseAllPositions() {
        return derived().CloseAllPositions_Impl();
    }

    bool IsMarketOpen() {
        return derived().IsMarketOpen_Impl();
    }

    // If market is closed, return next open time; if unknown, return nullopt.
    std::optional<SysTime> GetNextMarketOpenTime() {
        return derived().GetNextMarketOpenTime_Impl();
    }

protected:
    TradingApiBase() = default;

private:
    Derived& derived() { return static_cast<Derived&>(*this); }
    const Derived& derived() const { return static_cast<const Derived&>(*this); }

    static void validate_basic(std::string_view symbol, double qty) {
        if (symbol.empty()) throw std::invalid_argument("symbol must not be empty");
        if (qty <= 0.0) throw std::invalid_argument("qty must be > 0");
    }
};
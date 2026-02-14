// ChineseStrategy.h
#pragma once

#include <string>
#include <vector>
#include <memory>

#include "Contract.h"   // IBKR API
#include "Order.h"      // IBKR API

// ----------------------------
// Simple order DTOs
// ----------------------------
struct Instrument {
    // You can trade CSI300 via futures (CFFEX) or via Stock Connect products via SMART/HK, etc.
    // Keep it generic; map these fields to an IBKR Contract.
    std::string symbol;     // e.g. "IF" (CSI300 futures) or an ETF ticker
    std::string secType;    // "FUT", "STK", "ETF", "IND" (IND not tradable)
    std::string exchange;   // e.g. "CFFEX", "SEHK", "SSE", or "SMART"
    std::string currency;   // e.g. "CNH", "HKD", "CNY" (depends on venue)
    std::string lastTradeDateOrContractMonth; // FUT: "202603" or "20260315"
    std::string tradingClass; // optional for futures
    std::string multiplier;   // optional for futures, e.g. "300"
};

struct MarketOrder {
    Instrument instrument;
    double quantity = 0.0;
    bool isBuy = true; // true=BUY, false=SELL
    std::string tif = "DAY"; // "DAY", "GTC"
};

struct ShortOrder {
    Instrument instrument;
    double quantity = 0.0;
    // In IBKR, a "short" is typically just SELL with appropriate account permissions.
    std::string tif = "DAY";
};

struct StopOrder {
    Instrument instrument;
    double quantity = 0.0;
    bool isBuy = false; // commonly stop-loss is SELL for long positions
    double stopPrice = 0.0;
    std::string tif = "GTC";
};

struct LimitOrder {
    Instrument instrument;
    double quantity = 0.0;
    bool isBuy = true;
    double limitPrice = 0.0;
    std::string tif = "DAY";
};

// ----------------------------
// Results
// ----------------------------
struct OrderResult {
    bool ok = false;
    int orderId = -1;
    std::string message;
};

struct PositionCloseResult {
    bool ok = false;
    int closeOrdersSent = 0;
    std::vector<int> orderIds;
    std::string message;
};

// ----------------------------
// Minimal IBKR gateway abstraction
// (Implement this using EClientSocket/EWrapper in your project.)
// ----------------------------
struct OpenPosition {
    Instrument instrument;
    double position = 0.0;     // +long, -short
    double avgCost = 0.0;
};

class IIbkrGateway {
public:
    virtual ~IIbkrGateway() = default;

    // Must be connected and have received nextValidId already.
    virtual int nextOrderId() = 0;

    virtual void placeOrder(int orderId, const Contract& contract, const Order& order) = 0;

    // Provide a synchronous snapshot (your wrapper can internally wait for positionEnd()).
    virtual std::vector<OpenPosition> getOpenPositionsSnapshot() = 0;
};

// ----------------------------
// ChineseStrategy (IBKR)
// ----------------------------
class ChineseStrategy {
public:
    explicit ChineseStrategy(std::shared_ptr<IIbkrGateway> gateway);

    OrderResult PlaceMarketOrder_Impl(const MarketOrder& o);
    OrderResult PlaceShortOrder_Impl(const ShortOrder& o);
    OrderResult PlaceStopOrder_Impl(const StopOrder& o);
    OrderResult PlaceLimitOrder_Impl(const LimitOrder& o);

    PositionCloseResult CloseAllPositions_Impl();

private:
    Contract BuildContract(const Instrument& i) const;

    OrderResult SendOrder(const Contract& c, Order& o);

private:
    std::shared_ptr<IIbkrGateway> _gw;
};
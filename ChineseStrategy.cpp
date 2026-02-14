// ChineseStrategy.cpp
#include "ChineseStrategy.hpp"

#include <stdexcept>
#include <sstream>
#include <cmath>

static std::string ActionFromBuy(bool isBuy) {
    return isBuy ? "BUY" : "SELL";
}

ChineseStrategy::ChineseStrategy(std::shared_ptr<IIbkrGateway> gateway)
    : _gw(std::move(gateway)) {
    if (!_gw) throw std::invalid_argument("ChineseStrategy: gateway is null");
}

Contract ChineseStrategy::BuildContract(const Instrument& i) const {
    Contract c;
    c.symbol   = i.symbol;
    c.secType  = i.secType;
    c.exchange = i.exchange;
    c.currency = i.currency;

    // Futures
    if (i.secType == "FUT") {
        c.lastTradeDateOrContractMonth = i.lastTradeDateOrContractMonth;
        if (!i.tradingClass.empty()) c.tradingClass = i.tradingClass;
        if (!i.multiplier.empty())   c.multiplier   = i.multiplier;
    }

    // Stocks/ETFs typically only need symbol/secType/exchange/currency.
    // If you route via SMART, you may also want:
    // c.primaryExchange = "SSE"/"SEHK"/etc (depends on product)
    return c;
}

OrderResult ChineseStrategy::SendOrder(const Contract& c, Order& o) {
    OrderResult r;

    if (o.totalQuantity <= 0) {
        r.ok = false;
        r.message = "Quantity must be > 0";
        return r;
    }

    try {
        const int orderId = _gw->nextOrderId();
        _gw->placeOrder(orderId, c, o);

        r.ok = true;
        r.orderId = orderId;
        r.message = "Order sent to IBKR (acceptance/fill is async via EWrapper callbacks)";
        return r;
    } catch (const std::exception& e) {
        r.ok = false;
        r.message = std::string("Failed to send order: ") + e.what();
        return r;
    }
}

OrderResult ChineseStrategy::PlaceMarketOrder_Impl(const MarketOrder& in) {
    Contract c = BuildContract(in.instrument);

    Order o;
    o.action = ActionFromBuy(in.isBuy);
    o.orderType = "MKT";
    o.totalQuantity = in.quantity;
    o.tif = in.tif;

    return SendOrder(c, o);
}

OrderResult ChineseStrategy::PlaceShortOrder_Impl(const ShortOrder& in) {
    // Short = SELL market (or you can turn it into LMT if you want)
    Contract c = BuildContract(in.instrument);

    Order o;
    o.action = "SELL";
    o.orderType = "MKT";
    o.totalQuantity = in.quantity;
    o.tif = in.tif;

    // If you need to enforce "opening short only", you’d implement position checks here.
    return SendOrder(c, o);
}

OrderResult ChineseStrategy::PlaceStopOrder_Impl(const StopOrder& in) {
    if (!(in.stopPrice > 0.0)) {
        return OrderResult{false, -1, "StopPrice must be > 0"};
    }

    Contract c = BuildContract(in.instrument);

    Order o;
    o.action = ActionFromBuy(in.isBuy);
    o.orderType = "STP";
    o.totalQuantity = in.quantity;
    o.auxPrice = in.stopPrice; // IBKR uses auxPrice for stop price
    o.tif = in.tif;

    return SendOrder(c, o);
}

OrderResult ChineseStrategy::PlaceLimitOrder_Impl(const LimitOrder& in) {
    if (!(in.limitPrice > 0.0)) {
        return OrderResult{false, -1, "LimitPrice must be > 0"};
    }

    Contract c = BuildContract(in.instrument);

    Order o;
    o.action = ActionFromBuy(in.isBuy);
    o.orderType = "LMT";
    o.totalQuantity = in.quantity;
    o.lmtPrice = in.limitPrice;
    o.tif = in.tif;

    return SendOrder(c, o);
}

PositionCloseResult ChineseStrategy::CloseAllPositions_Impl() {
    PositionCloseResult out;

    std::vector<OpenPosition> positions;
    try {
        positions = _gw->getOpenPositionsSnapshot();
    } catch (const std::exception& e) {
        out.ok = false;
        out.message = std::string("Failed to fetch positions: ") + e.what();
        return out;
    }

    int sent = 0;
    std::vector<int> orderIds;

    for (const auto& p : positions) {
        if (std::fabs(p.position) < 1e-12) continue;

        // Close with opposite market order
        const bool closeBuy = (p.position < 0); // if short (-), BUY to close; if long (+), SELL to close
        MarketOrder mo;
        mo.instrument = p.instrument;
        mo.quantity = std::fabs(p.position);
        mo.isBuy = closeBuy;
        mo.tif = "DAY";

        auto res = PlaceMarketOrder_Impl(mo);
        if (res.ok) {
            sent++;
            orderIds.push_back(res.orderId);
        } else {
            // Best-effort: continue closing other positions
        }
    }

    out.ok = true;
    out.closeOrdersSent = sent;
    out.orderIds = std::move(orderIds);

    std::ostringstream oss;
    oss << "CloseAllPositions: sent " << sent << " market close orders.";
    out.message = oss.str();
    return out;
}

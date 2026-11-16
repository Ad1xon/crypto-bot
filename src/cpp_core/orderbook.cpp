// OrderBook implementation. The OFI / CVD formulas are explained in orderbook.hpp.
#include "orderbook.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <string>

namespace crypto_core {

namespace {

constexpr double kInf = std::numeric_limits<double>::infinity();

/// Bid at level `i`, or an empty {0, 0} level if the side is shorter than that.
inline Level bid_at(const Level* side, std::size_t n, std::size_t i) noexcept {
    return i < n ? side[i] : Level{0.0, 0.0};
}

/// Ask at level `i`, or an empty {+inf, 0} level if the side is shorter than that.
inline Level ask_at(const Level* side, std::size_t n, std::size_t i) noexcept {
    return i < n ? side[i] : Level{kInf, 0.0};
}

/// Calculate the current UTC day from the timestamp. Rounds down so it would
/// also work for negative timestamps (C++ `/` rounds towards zero).
inline std::int64_t utc_day(std::int64_t ts_ms) noexcept {
    std::int64_t day = ts_ms / kMsPerDay;
    if ((ts_ms % kMsPerDay) < 0) {
        --day;
    }
    return day;
}

}  // namespace

OrderBook::OrderBook(std::size_t depth, bool auto_reset_cvd) : depth_(depth), auto_reset_cvd_(auto_reset_cvd) {
    if (depth == 0 || depth > kMaxDepth) {
        throw std::invalid_argument("depth must be in [1, " + std::to_string(kMaxDepth) + "], got " +
                                    std::to_string(depth));
    }
}

std::size_t OrderBook::load_side(const double* src, std::size_t n_rows, Side& dst) const noexcept {
    std::size_t n = 0;
    for (std::size_t row = 0; row < n_rows && n < depth_; ++row) {
        const double price = src[2 * row];
        const double size = src[2 * row + 1];
        // Skip NaN/inf and empty levels so they don't mess up the OFI.
        if (!std::isfinite(price) || !std::isfinite(size) || size <= 0.0) {
            continue;
        }
        dst[n++] = Level{price, size};
    }
    return n;
}

void OrderBook::compute_ofi(std::size_t prev) noexcept {
    const Level* cb = bids_[cur_].data();
    const Level* ca = asks_[cur_].data();
    const Level* pb = bids_[prev].data();
    const Level* pa = asks_[prev].data();
    const std::size_t ncb = n_bids_[cur_];
    const std::size_t nca = n_asks_[cur_];
    const std::size_t npb = n_bids_[prev];
    const std::size_t npa = n_asks_[prev];

    for (std::size_t m = 0; m < depth_; ++m) {
        const Level b = bid_at(cb, ncb, m);
        const Level b0 = bid_at(pb, npb, m);
        const Level a = ask_at(ca, nca, m);
        const Level a0 = ask_at(pa, npa, m);

        // Multiplying by the comparison result instead of using if's lets the
        // compiler generate code without branches.
        double e = 0.0;
        e += static_cast<double>(b.price >= b0.price) * b.size;
        e -= static_cast<double>(b.price <= b0.price) * b0.size;
        e -= static_cast<double>(a.price <= a0.price) * a.size;
        e += static_cast<double>(a.price >= a0.price) * a0.size;
        ofi_levels_[m] = e;
    }
}

double OrderBook::update(const double* bids, std::size_t n_bids, const double* asks, std::size_t n_asks,
                         std::int64_t ts_ms) noexcept {
    const std::size_t prev = cur_;
    const std::size_t next = cur_ ^ 1U;

    n_bids_[next] = load_side(bids, n_bids, bids_[next]);
    n_asks_[next] = load_side(asks, n_asks, asks_[next]);
    cur_ = next;

    if (has_prev_) {
        compute_ofi(prev);
    } else {
        ofi_levels_.fill(0.0);
        has_prev_ = true;
    }

    cumulative_ofi_ += ofi_levels_[0];
    last_update_ms_ = ts_ms;
    ++update_count_;
    return ofi_levels_[0];
}

double OrderBook::apply_trade(double price, double size, bool is_buy, std::int64_t ts_ms) noexcept {
    if (!std::isfinite(size) || size < 0.0) {
        return cvd_;
    }

    if (auto_reset_cvd_ && ts_ms > 0) {
        const std::int64_t day = utc_day(ts_ms);
        if (day > last_trade_day_) {
            if (last_trade_day_ >= 0) {
                // First trade of a new day -> start CVD from zero.
                cvd_ = 0.0;
                buy_volume_ = 0.0;
                sell_volume_ = 0.0;
            }
            last_trade_day_ = day;
        }
    }

    if (is_buy) {
        cvd_ += size;
        buy_volume_ += size;
    } else {
        cvd_ -= size;
        sell_volume_ += size;
    }

    if (std::isfinite(price)) {
        last_trade_price_ = price;
    }
    last_trade_ms_ = ts_ms;
    ++trade_count_;
    return cvd_;
}

void OrderBook::reset_cvd(double value) noexcept {
    cvd_ = value;
    buy_volume_ = 0.0;
    sell_volume_ = 0.0;
}

void OrderBook::reset() noexcept {
    n_bids_.fill(0);
    n_asks_.fill(0);
    cur_ = 0;
    has_prev_ = false;
    ofi_levels_.fill(0.0);
    cumulative_ofi_ = 0.0;
    cvd_ = 0.0;
    buy_volume_ = 0.0;
    sell_volume_ = 0.0;
    last_trade_price_ = 0.0;
    last_trade_day_ = -1;
    update_count_ = 0;
    trade_count_ = 0;
    last_update_ms_ = 0;
    last_trade_ms_ = 0;
}

double OrderBook::mid_price() const noexcept {
    return is_two_sided() ? 0.5 * (best_bid_price() + best_ask_price()) : 0.0;
}

double OrderBook::spread() const noexcept {
    return is_two_sided() ? best_ask_price() - best_bid_price() : 0.0;
}

double OrderBook::microprice() const noexcept {
    if (!is_two_sided()) {
        return 0.0;
    }
    const double qb = best_bid_size();
    const double qa = best_ask_size();
    // load_side only keeps sizes > 0, so this can't divide by zero.
    return (best_bid_price() * qa + best_ask_price() * qb) / (qb + qa);
}

double OrderBook::depth_imbalance(std::size_t levels) const noexcept {
    const std::size_t nb = std::min({levels, depth_, bid_levels()});
    const std::size_t na = std::min({levels, depth_, ask_levels()});
    double bid_sum = 0.0;
    double ask_sum = 0.0;
    for (std::size_t i = 0; i < nb; ++i) {
        bid_sum += bids()[i].size;
    }
    for (std::size_t i = 0; i < na; ++i) {
        ask_sum += asks()[i].size;
    }
    const double total = bid_sum + ask_sum;
    return total > 0.0 ? (bid_sum - ask_sum) / total : 0.0;
}

double OrderBook::multi_level_ofi(std::size_t levels) const noexcept {
    const std::size_t n = std::min(levels, depth_);
    double sum = 0.0;
    for (std::size_t i = 0; i < n; ++i) {
        sum += ofi_levels_[i];
    }
    return sum;
}

}  // namespace crypto_core

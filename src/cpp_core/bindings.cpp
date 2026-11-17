// pybind11 wrapper that exposes crypto_core::OrderBook to Python as the
// `crypto_core` module.
//
// The fast entry points try not to create a Python object per level:
//   * update_book()        - NumPy (n, 2) float64 arrays, no copy if already C-contiguous.
//   * update_hyperliquid() - takes the raw `levels` list from a Hyperliquid `l2Book`
//                            message (dicts with string "px"/"sz") and parses it in
//                            C++ directly into stack buffers. No temporary tuples,
//                            lists or Python floats.
//   * apply_trades()       - CVD update for a whole `trades` message at once.
//
// We don't release the GIL here: a 20-level update takes less than a
// microsecond, which is faster than releasing and re-acquiring the GIL.
#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <string>

#include "orderbook.hpp"

#ifndef CRYPTO_CORE_VERSION
#define CRYPTO_CORE_VERSION "0.0.0+unknown"
#endif

namespace py = pybind11;
using crypto_core::kMaxDepth;
using crypto_core::Level;
using crypto_core::OrderBook;

namespace {

using InputArray = py::array_t<double, py::array::c_style | py::array::forcecast>;
using RowBuffer = std::array<double, 2 * kMaxDepth>;

/// Check that the array has shape (n, 2) and return n. An empty array (any
/// shape) just means that side of the book is empty.
std::size_t rows_of(const InputArray& arr, const char* name) {
    if (arr.size() == 0) {
        return 0;
    }
    if (arr.ndim() != 2 || arr.shape(1) != 2) {
        throw py::value_error(std::string(name) + " must have shape (n, 2) as [price, size] rows");
    }
    return static_cast<std::size_t>(arr.shape(0));
}

/// Convert a Python float / int / numeric string to double. Hyperliquid sends
/// prices as strings, so we parse those directly instead of creating a PyFloat first.
double to_double(PyObject* obj) {
    if (PyFloat_CheckExact(obj)) {
        return PyFloat_AS_DOUBLE(obj);
    }
    if (PyUnicode_Check(obj)) {
        Py_ssize_t len = 0;
        const char* text = PyUnicode_AsUTF8AndSize(obj, &len);
        if (text == nullptr) {
            throw py::error_already_set();
        }
        char* end = nullptr;
        const double value = PyOS_string_to_double(text, &end, nullptr);
        if (value == -1.0 && PyErr_Occurred()) {
            throw py::error_already_set();
        }
        if (len == 0 || end != text + len) {
            throw py::value_error("could not convert string to float: '" + std::string(text, static_cast<std::size_t>(len)) +
                                  "'");
        }
        return value;
    }
    const double value = PyFloat_AsDouble(obj);
    if (value == -1.0 && PyErr_Occurred()) {
        throw py::error_already_set();
    }
    return value;
}

/// Interned dict keys. They're created once and never freed on purpose, so we
/// don't allocate a new str for every dict lookup.
PyObject* interned(const char* key) {
    PyObject* s = PyUnicode_InternFromString(key);
    if (s == nullptr) {
        throw py::error_already_set();
    }
    return s;
}

PyObject* required_item(PyObject* dict, PyObject* key, const char* key_name) {
    PyObject* item = PyDict_GetItemWithError(dict, key);  // borrowed
    if (item == nullptr) {
        if (PyErr_Occurred()) {
            throw py::error_already_set();
        }
        throw py::key_error(std::string("level is missing '") + key_name + "'");
    }
    return item;
}

/// Parse one side of a Hyperliquid `levels` payload, i.e. a list of
/// {"px": "...", "sz": "...", "n": int} dicts. Reads at most `max_rows`.
std::size_t parse_hyperliquid_side(py::handle side, RowBuffer& out, std::size_t max_rows) {
    static PyObject* const kPx = interned("px");
    static PyObject* const kSz = interned("sz");

    py::object fast = py::reinterpret_steal<py::object>(PySequence_Fast(side.ptr(), "book side must be a sequence"));
    if (!fast) {
        throw py::error_already_set();
    }
    const Py_ssize_t n = PySequence_Fast_GET_SIZE(fast.ptr());
    PyObject** items = PySequence_Fast_ITEMS(fast.ptr());

    const std::size_t rows = std::min(static_cast<std::size_t>(n), max_rows);
    for (std::size_t i = 0; i < rows; ++i) {
        PyObject* level = items[i];
        if (!PyDict_Check(level)) {
            throw py::type_error("each book level must be a dict with 'px' and 'sz'");
        }
        out[2 * i] = to_double(required_item(level, kPx, "px"));
        out[2 * i + 1] = to_double(required_item(level, kSz, "sz"));
    }
    return rows;
}

/// Copy one side of the book into a fresh (n, 2) NumPy array.
py::array_t<double> side_to_array(const Level* levels, std::size_t n) {
    py::array_t<double> out({static_cast<py::ssize_t>(n), static_cast<py::ssize_t>(2)});
    auto view = out.mutable_unchecked<2>();
    for (std::size_t i = 0; i < n; ++i) {
        view(static_cast<py::ssize_t>(i), 0) = levels[i].price;
        view(static_cast<py::ssize_t>(i), 1) = levels[i].size;
    }
    return out;
}

}  // namespace

PYBIND11_MODULE(crypto_core, m) {
    m.doc() =
        "crypto_core: C++17 L2 order book that tracks CVD and OFI.\n\n"
        "Faster replacement for the order book logic in HyperliquidStreamer.";
    m.attr("__version__") = CRYPTO_CORE_VERSION;
    m.attr("MAX_DEPTH") = kMaxDepth;

    py::class_<OrderBook>(m, "OrderBook", R"doc(
L2 order book (up to 20 levels per side) that updates CVD and OFI on every message.

OFI is computed for each level, so you can also sum it over several levels.
CVD is buy volume minus sell volume, optionally reset at the start of each UTC day.
Not thread-safe: use one instance per symbol.
)doc")
        .def(py::init<std::size_t, bool>(), py::arg("depth") = kMaxDepth, py::arg("auto_reset_cvd") = true,
             "Create a book retaining ``depth`` levels per side (1..20).")

        // ---------------------------------------------------------------- ingest
        .def(
            "update_book",
            [](OrderBook& self, const InputArray& bids, const InputArray& asks, std::int64_t timestamp_ms) {
                const std::size_t nb = rows_of(bids, "bids");
                const std::size_t na = rows_of(asks, "asks");
                return self.update(bids.data(), nb, asks.data(), na, timestamp_ms);
            },
            py::arg("bids"), py::arg("asks"), py::arg("timestamp_ms") = 0,
            "Apply a full snapshot from (n, 2) [price, size] arrays (best level first). Returns level-1 OFI.")
        .def(
            "update_hyperliquid",
            [](OrderBook& self, const py::sequence& levels, std::int64_t timestamp_ms) {
                RowBuffer bid_rows{};
                RowBuffer ask_rows{};
                const auto n_sides = static_cast<std::size_t>(py::len(levels));
                const std::size_t nb = n_sides > 0 ? parse_hyperliquid_side(levels[0], bid_rows, self.depth()) : 0;
                const std::size_t na = n_sides > 1 ? parse_hyperliquid_side(levels[1], ask_rows, self.depth()) : 0;
                return self.update(bid_rows.data(), nb, ask_rows.data(), na, timestamp_ms);
            },
            py::arg("levels"), py::arg("timestamp_ms") = 0,
            "Apply the raw ``data['levels']`` list of a Hyperliquid l2Book message. Returns level-1 OFI.")
        .def("apply_trade", &OrderBook::apply_trade, py::arg("price"), py::arg("size"), py::arg("is_buy"),
             py::arg("timestamp_ms") = 0, "Apply one aggressor trade. Returns CVD after the trade.")
        .def(
            "apply_trades",
            [](OrderBook& self, const InputArray& prices, const InputArray& sizes,
               const py::array_t<bool, py::array::c_style | py::array::forcecast>& is_buy,
               const py::array_t<std::int64_t, py::array::c_style | py::array::forcecast>& timestamps_ms) {
                const py::ssize_t n = prices.size();
                if (prices.ndim() > 1 || sizes.ndim() > 1 || is_buy.ndim() > 1 || timestamps_ms.ndim() > 1) {
                    throw py::value_error("apply_trades expects 1-D arrays");
                }
                if (sizes.size() != n || is_buy.size() != n || timestamps_ms.size() != n) {
                    throw py::value_error("prices, sizes, is_buy and timestamps_ms must have equal length");
                }
                const double* px = prices.data();
                const double* sz = sizes.data();
                const bool* buy = is_buy.data();
                const std::int64_t* ts = timestamps_ms.data();
                for (py::ssize_t i = 0; i < n; ++i) {
                    self.apply_trade(px[i], sz[i], buy[i], ts[i]);
                }
                return self.cvd();
            },
            py::arg("prices"), py::arg("sizes"), py::arg("is_buy"), py::arg("timestamps_ms"),
            "apply_trade for a batch of trades given as 1-D arrays. Returns CVD after the last trade.")
        .def("reset_cvd", &OrderBook::reset_cvd, py::arg("value") = 0.0,
             "Set CVD to ``value`` and clear buy/sell volume accumulators.")
        .def("reset", &OrderBook::reset, "Clear all book, OFI and CVD state (e.g. after a reconnect).")

        // --------------------------------------------------------------- queries
        .def_property_readonly("depth", &OrderBook::depth)
        .def_property_readonly("auto_reset_cvd", &OrderBook::auto_reset_cvd)
        .def_property_readonly("bid_levels", &OrderBook::bid_levels)
        .def_property_readonly("ask_levels", &OrderBook::ask_levels)
        .def_property_readonly("best_bid_price", &OrderBook::best_bid_price)
        .def_property_readonly("best_bid_size", &OrderBook::best_bid_size)
        .def_property_readonly("best_ask_price", &OrderBook::best_ask_price)
        .def_property_readonly("best_ask_size", &OrderBook::best_ask_size)
        .def_property_readonly("is_two_sided", &OrderBook::is_two_sided)
        .def_property_readonly("mid_price", &OrderBook::mid_price)
        .def_property_readonly("spread", &OrderBook::spread)
        .def_property_readonly("microprice", &OrderBook::microprice)
        .def_property_readonly("top_imbalance", &OrderBook::top_imbalance,
                               "best_bid_size - best_ask_size (the legacy streamer's 'ofi' field).")
        .def_property_readonly("ofi", &OrderBook::ofi, "Level-1 OFI of the most recent update.")
        .def_property_readonly("cumulative_ofi", &OrderBook::cumulative_ofi)
        .def_property_readonly("cvd", &OrderBook::cvd)
        .def_property_readonly("buy_volume", &OrderBook::buy_volume)
        .def_property_readonly("sell_volume", &OrderBook::sell_volume)
        .def_property_readonly("last_trade_price", &OrderBook::last_trade_price)
        .def_property_readonly("update_count", &OrderBook::update_count)
        .def_property_readonly("trade_count", &OrderBook::trade_count)
        .def_property_readonly("last_update_ms", &OrderBook::last_update_ms)
        .def_property_readonly("last_trade_ms", &OrderBook::last_trade_ms)
        .def("depth_imbalance", &OrderBook::depth_imbalance, py::arg("levels") = kMaxDepth,
             "(sum bid - sum ask) / (sum bid + sum ask) over the first ``levels`` levels.")
        .def("multi_level_ofi", &OrderBook::multi_level_ofi, py::arg("levels") = kMaxDepth,
             "Sum of per-level OFI over the first ``levels`` levels.")
        .def(
            "ofi_levels",
            [](const OrderBook& self) {
                py::array_t<double> out(static_cast<py::ssize_t>(self.depth()));
                std::copy_n(self.ofi_levels(), self.depth(), out.mutable_data());
                return out;
            },
            "Per-level OFI of the most recent update as a 1-D array of length ``depth``.")
        .def(
            "bids", [](const OrderBook& self) { return side_to_array(self.bids(), self.bid_levels()); },
            "Copy of the bid side as an (n, 2) [price, size] array.")
        .def(
            "asks", [](const OrderBook& self) { return side_to_array(self.asks(), self.ask_levels()); },
            "Copy of the ask side as an (n, 2) [price, size] array.")
        .def(
            "snapshot",
            [](const OrderBook& self) {
                py::dict d;
                d["timestamp_ms"] = self.last_update_ms();
                d["best_bid_price"] = self.best_bid_price();
                d["best_bid_qty"] = self.best_bid_size();
                d["best_ask_price"] = self.best_ask_price();
                d["best_ask_qty"] = self.best_ask_size();
                d["spread"] = self.spread();
                d["mid_price"] = self.mid_price();
                d["microprice"] = self.microprice();
                d["ofi"] = self.ofi();
                d["top_imbalance"] = self.top_imbalance();
                d["cvd"] = self.cvd();
                return d;
            },
            "Dict with BBO + metrics. Contains all the keys TickLogger.put_bbo() expects.")
        .def("__repr__", [](const OrderBook& self) {
            return "<crypto_core.OrderBook depth=" + std::to_string(self.depth()) +
                   " bid=" + std::to_string(self.best_bid_price()) + " ask=" + std::to_string(self.best_ask_price()) +
                   " cvd=" + std::to_string(self.cvd()) + " ofi=" + std::to_string(self.ofi()) + ">";
        });
}

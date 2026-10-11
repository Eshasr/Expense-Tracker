/* ==========================================================================
   SpendWise - main.js
   - Confirm dialogs for destructive forms  (form[data-confirm])
   - Double-submit protection on all forms
   - Auto-submit filters                    ([data-autosubmit])
   - Auto-hide success/info flash messages
   - Dashboard charts and insights          (SpendWise.initDashboard)
   ========================================================================== */

(function () {
    "use strict";

    var COLORS = {
        primary: "#1d4ed8",
        income: "#15803d",
        expense: "#b91c1c",
        grid: "#e2e8f0",
        text: "#475569"
    };

    var PALETTE = [
        "#1d4ed8", "#b45309", "#15803d", "#7c3aed", "#b91c1c",
        "#0e7490", "#be185d", "#475569", "#4d7c0f", "#c2410c"
    ];

    var money = new Intl.NumberFormat("en-IN", {
        minimumFractionDigits: 0,
        maximumFractionDigits: 2
    });

    function rupees(value) {
        return "\u20B9" + money.format(value);
    }

    /* ---------- Generic page behaviour ---------------------------------- */

    function initForms() {
        var forms = document.querySelectorAll("form");
        forms.forEach(function (form) {
            form.addEventListener("submit", function (event) {
                var message = form.getAttribute("data-confirm");
                if (message && !window.confirm(message)) {
                    event.preventDefault();
                    return;
                }
                // Disable submit buttons after the browser has collected the
                // form data, to prevent accidental double submissions.
                window.setTimeout(function () {
                    form.querySelectorAll('button[type="submit"]').forEach(function (btn) {
                        btn.disabled = true;
                    });
                }, 0);
            });
        });
    }

    function initAutoSubmit() {
        document.querySelectorAll("[data-autosubmit]").forEach(function (el) {
            el.addEventListener("change", function () {
                if (el.form) {
                    el.form.submit();
                }
            });
        });
    }

    function initFlashMessages() {
        var alerts = document.querySelectorAll(".alert-success, .alert-info");
        alerts.forEach(function (alertBox) {
            window.setTimeout(function () {
                alertBox.style.transition = "opacity 0.4s";
                alertBox.style.opacity = "0";
                window.setTimeout(function () {
                    if (alertBox.parentNode) {
                        alertBox.parentNode.removeChild(alertBox);
                    }
                }, 400);
            }, 5000);
        });
    }

    /* ---------- Dashboard ----------------------------------------------- */

    function fetchJSON(url) {
        return fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
            .then(function (response) {
                if (!response.ok) {
                    throw new Error("Request failed (" + response.status + ")");
                }
                return response.json();
            });
    }

    function showMessage(boxId, text) {
        var box = document.getElementById(boxId);
        if (box) {
            box.innerHTML = '<p class="empty-state"></p>';
            box.firstChild.textContent = text;
        }
    }

    function baseOptions() {
        return {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { labels: { color: COLORS.text, boxWidth: 14 } },
                tooltip: {
                    callbacks: {
                        label: function (ctx) {
                            var label = ctx.dataset.label || ctx.label || "";
                            var value = ctx.parsed && ctx.parsed.y !== undefined ? ctx.parsed.y : ctx.parsed;
                            return label + ": " + rupees(value);
                        }
                    }
                }
            }
        };
    }

    function axisOptions() {
        return {
            x: { ticks: { color: COLORS.text }, grid: { display: false } },
            y: {
                beginAtZero: true,
                ticks: {
                    color: COLORS.text,
                    callback: function (v) { return rupees(v); }
                },
                grid: { color: COLORS.grid }
            }
        };
    }

    function drawCategoryChart(data) {
        if (!data.labels.length) {
            showMessage("categoryBox", "No expenses recorded this month.");
            return;
        }
        var options = baseOptions();
        options.plugins.legend.position = "bottom";
        options.plugins.tooltip.callbacks.label = function (ctx) {
            return ctx.label + ": " + rupees(ctx.parsed);
        };
        new Chart(document.getElementById("categoryChart"), {
            type: "pie",
            data: {
                labels: data.labels,
                datasets: [{
                    data: data.values,
                    backgroundColor: data.labels.map(function (_, i) { return PALETTE[i % PALETTE.length]; }),
                    borderColor: "#ffffff",
                    borderWidth: 2
                }]
            },
            options: options
        });
    }

    function drawDailyChart(data) {
        if (!data.labels.length) {
            showMessage("dailyBox", "No daily spending to show yet.");
            return;
        }
        var options = baseOptions();
        options.plugins.legend.display = false;
        options.scales = axisOptions();
        new Chart(document.getElementById("dailyChart"), {
            type: "bar",
            data: {
                labels: data.labels.map(function (d) { return d.slice(8); }), // day of month
                datasets: [{
                    label: "Spent",
                    data: data.values,
                    backgroundColor: COLORS.primary
                }]
            },
            options: options
        });
    }

    function drawTrendChart(data) {
        if (!data.labels.length) {
            showMessage("trendBox", "Add transactions to see your monthly trend.");
            return;
        }
        var options = baseOptions();
        options.plugins.legend.position = "bottom";
        options.scales = axisOptions();
        new Chart(document.getElementById("trendChart"), {
            type: "line",
            data: {
                labels: data.labels,
                datasets: [
                    {
                        label: "Income",
                        data: data.income,
                        borderColor: COLORS.income,
                        backgroundColor: COLORS.income,
                        tension: 0.2
                    },
                    {
                        label: "Expenses",
                        data: data.expense,
                        borderColor: COLORS.expense,
                        backgroundColor: COLORS.expense,
                        tension: 0.2
                    }
                ]
            },
            options: options
        });
    }

    function drawInsights(messages) {
        var list = document.getElementById("insightsList");
        if (!list) { return; }
        list.innerHTML = "";
        messages.forEach(function (text) {
            var li = document.createElement("li");
            li.textContent = text;
            list.appendChild(li);
        });
    }

    function initDashboard() {
        var root = document.getElementById("dashboard");
        if (!root) { return; }

        var month = root.getAttribute("data-month") || "";
        var query = month ? "?month=" + encodeURIComponent(month) : "";

        if (typeof Chart === "undefined") {
            showMessage("categoryBox", "Charts could not be loaded. Check your internet connection.");
            showMessage("dailyBox", "Charts could not be loaded.");
            showMessage("trendBox", "Charts could not be loaded.");
        } else {
            fetchJSON("/api/chart-data" + query)
                .then(function (data) {
                    drawCategoryChart(data.category);
                    drawDailyChart(data.daily);
                    drawTrendChart(data.trend);
                })
                .catch(function () {
                    showMessage("categoryBox", "Could not load chart data.");
                    showMessage("dailyBox", "Could not load chart data.");
                    showMessage("trendBox", "Could not load chart data.");
                });
        }

        fetchJSON("/api/insights" + query)
            .then(function (data) { drawInsights(data.insights || []); })
            .catch(function () { drawInsights(["Could not load insights right now."]); });
    }

    /* ---------- Boot ---------------------------------------------------- */

    document.addEventListener("DOMContentLoaded", function () {
        initForms();
        initAutoSubmit();
        initFlashMessages();
    });

    window.SpendWise = { initDashboard: initDashboard };
})();

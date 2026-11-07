(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { useState, useEffect, useRef } = SDK.hooks;
  const h = React.createElement;
  const API = "/api/plugins/emporium";

  const LABEL = { settled: "Delivered · paid", working: "Working", paid: "Paid", failed: "Not charged",
    unpaid: "Unsettled", refused: "Refused", bought: "Bought", not_charged: "Not charged", pending: "Paying" };

  function short(a) { return a && a.length > 12 ? a.slice(0, 6) + "…" + a.slice(-4) : (a || "—"); }
  function ago(ts) {
    if (!ts) return "";
    const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    return s < 60 ? s + "s ago" : s < 3600 ? Math.floor(s / 60) + "m ago" : s < 86400 ? Math.floor(s / 3600) + "h ago" : Math.floor(s / 86400) + "d ago";
  }
  function money(n) { return "$" + n.toFixed(2); }
  function pct(r) { return r == null ? "—" : (r * 100).toFixed(r > 0.995 && r < 1 ? 1 : 0) + "%"; }

  // Animated number: eases from the last shown value to the new one.
  function useCountUp(target) {
    const [shown, setShown] = useState(target);
    const from = useRef(target);
    useEffect(() => {
      const start = performance.now(), a = from.current, b = target;
      if (a === b) return;
      let raf;
      const step = (t) => {
        const k = Math.min(1, (t - start) / 900), e = 1 - Math.pow(1 - k, 3);
        setShown(a + (b - a) * e);
        if (k < 1) raf = requestAnimationFrame(step); else from.current = b;
      };
      raf = requestAnimationFrame(step);
      return () => cancelAnimationFrame(raf);
    }, [target]);
    return shown;
  }

  function Pill(props) { return h("span", { className: "emp-pill " + (props.kind || "") }, props.children); }

  function Status(props) {
    return h("span", { className: "emp-st emp-st-" + props.s }, LABEL[props.s] || props.s);
  }

  function EmporiumPage() {
    const [data, setData] = useState(null);
    const [err, setErr] = useState("");
    const [toast, setToast] = useState(null);
    const [flash, setFlash] = useState(false);
    const seen = useRef(null);

    async function load() {
      try {
        const d = await SDK.fetchJSON(API + "/summary");
        const settled = new Set(d.jobs.filter((j) => j.status === "settled").map((j) => j.id));
        if (seen.current) {
          const fresh = d.jobs.filter((j) => j.status === "settled" && !seen.current.has(j.id));
          if (fresh.length) {
            setToast({ amount: fresh[0].price, title: fresh[0].title, n: fresh.length, key: Date.now() });
            setFlash(true);
            setTimeout(() => setFlash(false), 1400);
            setTimeout(() => setToast(null), 3600);
          }
        }
        seen.current = settled;
        setData(d);
        setErr("");
      } catch (e) {
        setErr(String((e && e.message) || e));
      }
    }

    useEffect(() => { load(); const t = setInterval(load, 2000); return () => clearInterval(t); }, []);

    const earned = useCountUp(data ? parseFloat(data.sales.earned_total_raw || "0") : 0);
    const profit = useCountUp(data ? parseFloat(data.sales.profit_raw || "0") : 0);

    async function toggle() {
      if (!data) return;
      await SDK.fetchJSON(API + "/open", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ open: !data.shop.open }) });
      load();
    }

    if (!data) return h("div", { className: "emp" }, h("div", { className: "emp-card emp-empty" }, err ? "Emporium: " + err : "Opening the books…"));
    const { shop, sales, menu, jobs, buying } = data;

    const hero = h("section", { className: "emp-hero" + (flash ? " emp-flash" : "") },
      h("div", { className: "emp-id" },
        h("div", { className: "emp-mark" }, "H"),
        h("div", null,
          h("h1", null, shop.name),
          h("p", null, shop.tagline),
          h("div", { className: "emp-pills" },
            h(Pill, { kind: shop.open ? "ok" : "bad" }, shop.open ? "● Open for business" : "● Closed"),
            h(Pill, null, "USDC on " + shop.network), shop.testnet ? h(Pill, { kind: "gold" }, "testnet") : null,
            h("a", { className: "emp-pill emp-link", href: shop.url, target: "_blank", rel: "noopener" }, shop.url.replace(/^https?:\/\//, "") + " ↗"),
            h(Pill, null, "wallet " + short(shop.pay_to)),
            (shop.risks && shop.risks.length) ? null : h(Pill, { kind: "ok" }, "sealed profile · " + shop.profile)))),
      h("div", { className: "emp-earn" },
        h("div", { className: "emp-lbl" }, "Earned"),
        h("div", { className: "emp-big" }, money(earned)),
        h("div", { className: "emp-sub" }, sales.earned_today + " today · " + sales.jobs_settled + (sales.jobs_settled === 1 ? " job · " : " jobs · ") + sales.buyers + (sales.buyers === 1 ? " buyer" : " buyers")),
        sales.margin != null ? h("div", { className: "emp-profit" }, h("b", null, money(profit)), " profit · " + pct(sales.margin) + " margin") : null,
        h("button", { className: "emp-btn", onClick: toggle }, shop.open ? "Close shop" : "Open shop")));

    const problems = !shop.ready ? h("div", { className: "emp-card emp-warn" }, "Not ready: " + shop.problems.join("; "))
      : (shop.risks && shop.risks.length) ? h("div", { className: "emp-card emp-warn emp-soft-warn" },
          shop.risks.map((r) => h("div", { key: r }, "⚠ " + r.charAt(0).toUpperCase() + r.slice(1) + ".")),
          h("div", { className: "emp-hint" }, "Tools stay locked. To seal it, " + shop.seal_hint + ".")) : null;

    const live = h("div", { className: "emp-card emp-live" },
      h("div", { className: "emp-card-h" }, h("h2", null, "Live jobs"), h("span", { className: "emp-dot" })),
      jobs.length ? h("div", { className: "emp-table" },
        h("div", { className: "emp-row emp-head" }, h("span", null, "Job"), h("span", null, "Buyer"), h("span", null, "Price"), h("span", null, "Status"), h("span", null, "Receipt")),
        jobs.slice(0, 12).map((j) => h("div", { key: j.id + j.status, className: "emp-row emp-in" },
          h("span", null, j.title, j.blocked ? h("em", { className: "emp-blocked", title: "tool calls refused by the shop lock" }, " · " + j.blocked + " refused") : null),
          h("span", { className: "emp-mono" }, short(j.payer)),
          h("span", { className: "emp-mono emp-pc" }, j.price, j.cost ? h("small", { title: "inference cost, from Hermes's own session pricing" }, "cost " + j.cost) : null,
            j.sub ? h("small", { title: "paid to other shops for this job" }, "sub " + j.sub) : null),
          h("span", { className: "emp-stc" }, h(Status, { s: j.status }), j.seconds ? h("small", null, j.seconds + "s") : null),
          h("span", { className: "emp-mono" }, j.explorer ? h("a", { href: j.explorer, target: "_blank", rel: "noopener" }, j.tx.slice(0, 10) + "…") : h("small", null, ago(j.created))))))
        : h("div", { className: "emp-empty" }, "No jobs yet. Share " + shop.url + " with a buyer."));

    const maxSold = Math.max(1, ...menu.map((m) => m.sold));
    const menuCard = h("div", { className: "emp-card" },
      h("div", { className: "emp-card-h" }, h("h2", null, "Menu")),
      menu.map((m) => h("div", { key: m.id, className: "emp-menu" },
        h("div", { className: "emp-menu-top" }, h("b", null, m.title), h("span", { className: "emp-price" }, m.price)),
        h("div", { className: "emp-bar" }, h("i", { style: { width: (100 * m.sold / maxSold) + "%" } })),
        h("small", null, m.sold + " sold · " + m.earned + (m.tools.length ? " · tools: " + m.tools.join(", ") : " · no tools")))));

    const ratio = Math.min(1, buying.spent_ratio || 0);
    const hiring = h("div", { className: "emp-card" },
      h("div", { className: "emp-card-h" }, h("h2", null, "Hiring other shops")),
      h("div", { className: "emp-budget" }, h("span", null, buying.spent_today + " of " + buying.budget + " today"),
        h("small", null, "max " + buying.max_per_job + " a job · asks you above " + buying.approve_over)),
      h("div", { className: "emp-bar emp-bar-budget" }, h("i", { style: { width: (100 * ratio) + "%" } })),
      buying.purchases.length ? buying.purchases.slice(0, 6).map((b) => h("div", { key: b.id, className: "emp-buy" },
        h("span", null, b.service, h("small", null, " @ " + b.shop), b.for_job ? h("em", { className: "emp-sc", title: "subcontracted by one of this shop's own paid jobs" }, "subcontract") : null),
        h("span", { className: "emp-mono" }, b.explorer ? h("a", { href: b.explorer, target: "_blank", rel: "noopener" }, b.price) : b.price), h(Status, { s: b.status })))
        : h("div", { className: "emp-empty" }, "Nothing bought yet."));

    const m = sales.margin;
    const books = h("div", { className: "emp-card emp-books" },
      h("div", { className: "emp-card-h" }, h("h2", null, "The books"), sales.covers ? h("span", { className: "emp-cover" }, "earns " + Math.floor(sales.covers) + "× its AI bill") : null),
      h("div", { className: "emp-ledger" },
        h("div", null, h("span", null, "Sales"), h("b", { className: "emp-mono" }, sales.earned_total)),
        h("div", null, h("span", null, "AI cost", h("small", null, " · Hermes's own estimate")), h("b", { className: "emp-mono emp-neg" }, sales.inference_cost ? "−" + sales.inference_cost : "—")),
        h("div", null, h("span", null, "Paid to other shops"), h("b", { className: "emp-mono emp-neg" }, sales.subcontract_cost === "$0.00" ? "$0.00" : "−" + sales.subcontract_cost)),
        h("div", { className: "emp-total" }, h("span", null, "Profit"), h("b", { className: "emp-mono" }, sales.profit))),
      h("div", { className: "emp-bar emp-bar-margin" }, h("i", { style: { width: (100 * Math.max(0, Math.min(1, m || 0))) + "%" } })),
      h("small", { className: "emp-quality" }, (m == null ? "Margin appears after the first priced job" : pct(m) + " margin") +
        (sales.delivery_rate != null ? " · " + pct(sales.delivery_rate) + " delivered" : "") +
        (sales.p50_seconds != null ? " · median " + sales.p50_seconds + "s" : "") +
        (sales.tokens ? " · " + (sales.tokens >= 1e6 ? (sales.tokens / 1e6).toFixed(1) + "M" : Math.round(sales.tokens / 1000) + "k") + " tokens" : "")));

    const safety = h("div", { className: "emp-card" },
      h("div", { className: "emp-card-h" }, h("h2", null, "Sealed jobs")),
      h("div", { className: "emp-stats" },
        h("div", null, h("b", null, String(sales.refused_tools)), h("small", null, "tool calls refused")),
        h("div", null, h("b", null, String(sales.not_charged)), h("small", null, "jobs not charged")),
        h("div", null, h("b", null, String(sales.jobs_settled)), h("small", null, "paid on delivery"))),
      h("p", { className: "emp-note" }, "Every job runs in its own session with only the tools its service sells. Buyers pay only when the work is delivered."));

    return h("div", { className: "emp" }, hero, problems,
      h("div", { className: "emp-grid" }, live, menuCard),
      h("div", { className: "emp-grid emp-grid-3" }, books, hiring, safety),
      toast ? h("div", { key: toast.key, className: "emp-toast" }, h("b", null, "+" + toast.amount), h("span", null, toast.title + " · paid on delivery")) : null);
  }

  window.__HERMES_PLUGINS__.register("emporium", EmporiumPage);
})();

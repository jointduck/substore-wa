/* ═══════════════════════════════════════════════════════════════
   SUBSTORE Mini App — v25, ПОЛНОФУНКЦИОНАЛЬНЫЙ магазин.
   Паритет с ботом: каталог → тариф → оформление (промокод, бонус) →
   оплата (Gram / USDT / карта Tribute-Digiseller / Stars) → статус с
   живым поллингом → данные аккаунта → история заказов → профиль с
   рефералкой. Чат остаётся запасным каналом: те же заказы, те же
   поллеры бота.
   Дизайн: ton.org / gramcoin.org (тёмный, сине-градиентный).
   ═══════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  /* ── Конфиг по умолчанию (переопределяется /api/session) ── */
  var DEFAULTS = {
    store_name: "SUBSTORE",
    bot_username: "Indiasubbot",
    offer_url: "https://disk.yandex.ru/i/HWpCZ1blH8fyUw"
  };
  var DEFAULT_CONFIG = {
    methods: ["ton", "usdt", "card", "stars"],
    card_provider: "tribute",
    stars_per_usdt: 100,
    wallet_configured: true,
    promo_enabled: true
  };

  /* ── Иконки сервисов: SVG-спрайт (#i-*) + градиентные плашки ── */
  var SERVICE_ART = {
    spotify_premium: { icon: "spotify",    emoji: "🎵", a: "#1ED760", b: "#128C3E" },
    youtube_premium: { icon: "youtube",    emoji: "▶️", a: "#FF4E45", b: "#C9001B" },
    apple_music:     { icon: "applemusic", emoji: "🎧", a: "#FB5C74", b: "#FA233B" },
    chatgpt_plus:    { icon: "openai",     emoji: "🤖", a: "#1BB99A", b: "#0B7A61" },
    netflix:         { icon: "netflix",    emoji: "🎬", a: "#E50914", b: "#7A050D" }
  };

  /* ── Способы оплаты: иконка, название, подпись, цвета ── */
  var METHOD_ART = {
    ton:   { icon: "ton",    emoji: "💎", name: "Gram",             sub: "Криптовалюта — моментально", a: "#1EAEFB", b: "#2286D3" },
    usdt:  { icon: "tether", emoji: "💵", name: "USDT (Tether)",    sub: "Стейблкоин в сети Gram",     a: "#26A17B", b: "#17775A" },
    card:  { icon: "card",   emoji: "💳", name: "Банковская карта", sub: "Карта или СБП",              a: "#7BA7E8", b: "#4C7BC8" },
    stars: { icon: "star",   emoji: "⭐", name: "Telegram Stars",   sub: "Оплата прямо в Telegram",    a: "#F5C64D", b: "#E09A1E" }
  };
  var CARD_SUB = {
    tribute:    "Карты и СБП — оплата в Telegram",
    digiseller: "Оплата картой через Digiseller"
  };

  /* ── Кастомные эмодзи (tg-emoji) ──
     Telegram рендерит <tg-emoji> в Mini App ТОЛЬКО если эмодзи из пака,
     принадлежащего боту. ID ниже — из emojis.py (запасной источник:
     каталог catalog.json → custom_emoji_id).

     ВКЛЮЧЕНИЕ: загрузите пак боту (python upload_custom_emoji.py после
     добавления OWNER_USER_ID в .env), впишите выданные ID в CUSTOM_EMOJI_IDS
     и поставьте USE_CUSTOM_EMOJI = true. Пока false — показываются SVG
     (гарантированно, у всех пользователей).                                  */
  var USE_CUSTOM_EMOJI = false;
  var CUSTOM_EMOJI_IDS = {
    spotify: "5891249688933305846",
    youtube: "5206230030450975877",
    applemusic: "5818920837645867167",
    openai: "5945217417591397712",
    netflix: "6005986106703613755",
    ton: "5449683594425410231",
    tether: "5409048419211682843",
    card: "5927169041595634481",
    star: "5438496463044752972",
    check: "5206607081334906820",
    sparkle: "5438496463044752972",
    fire: "5424972470023104089",
    gift: "5222444124698853913"
  };

  function iconTag(name) {
    return '<svg class="ico" aria-hidden="true"><use href="#i-' + name +
      '" xlink:href="#i-' + name + '"></use></svg>';
  }

  /* Иконка сервиса: кастомный эмодзи (если включено и есть ID)
     или SVG из спрайта (гарантированный вариант). */
  function svcIconHTML(svc) {
    var art = SERVICE_ART[svc.id] || { icon: "sparkle", emoji: svc.emoji || "⭐" };
    if (USE_CUSTOM_EMOJI && tg && !DEMO) {
      var eid = svc.custom_emoji_id || CUSTOM_EMOJI_IDS[art.icon] || "";
      if (eid) {
        return '<tg-emoji emoji-id="' + eid + '" data-icon="' + art.icon + '">' +
          art.emoji + "</tg-emoji>";
      }
    }
    return iconTag(art.icon);
  }

  /* Иконка способа оплаты — тот же механизм tg-emoji с SVG-фоллбэком */
  function methodIconHTML(m, size) {
    var art = METHOD_ART[m] || METHOD_ART.stars;
    if (USE_CUSTOM_EMOJI && tg && !DEMO) {
      var eid = CUSTOM_EMOJI_IDS[art.icon] || "";
      if (eid) {
        return '<tg-emoji emoji-id="' + eid + '" data-icon="' + art.icon + '">' +
          art.emoji + "</tg-emoji>";
      }
    }
    return iconTag(art.icon);
  }

  /* Если Telegram не отрисовал кастомный эмодзи — заменяем на SVG-иконку. */
  function watchCustomEmoji() {
    if (!USE_CUSTOM_EMOJI) return;
    [700, 1800].forEach(function (ms) {
      setTimeout(function () {
        var nodes = document.querySelectorAll("tg-emoji[data-icon]");
        for (var i = 0; i < nodes.length; i++) {
          var n = nodes[i];
          if (!n.querySelector("img")) {
            n.innerHTML = iconTag(n.getAttribute("data-icon"));
            n.removeAttribute("data-icon");
          }
        }
      }, ms);
    });
  }

  function hexA(hex, a) {
    var n = parseInt(hex.slice(1), 16);
    return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }
  function brandOf(svc) {
    var art = SERVICE_ART[svc.id] || { icon: "sparkle", emoji: svc.emoji || "⭐", a: "#378EE5", b: "#1EAEFB" };
    return {
      art: art,
      color: art.a,
      soft: hexA(art.a, .13),
      line: hexA(art.a, .38)
    };
  }

  /* ── Telegram WebApp / demo-режим ── */
  var tg = (window.Telegram && window.Telegram.WebApp) ? window.Telegram.WebApp : null;
  var DEMO = !tg || !tg.initData;

  /* v27: демо-шлюз — приложение открыто вне Telegram.
     Покупки невозможны (нужна подпись initData), поэтому вместо тостов
     показываем внятный экран с кнопкой «Открыть магазин в Telegram». */
  function demoGateLink() {
    var u = (state.cfg && state.cfg.bot_username) || "";
    return u ? "https://t.me/" + u + "?startapp=cat"
             : "https://t.me/"; /* username ещё не пришли — откроем Telegram */
  }
  function showDemoGate() {
    var g = $("demoGate");
    if (!g) return;
    var open = $("demoGateOpen");
    if (open) open.href = demoGateLink();
    g.hidden = false;
    document.body.classList.add("gate-open");
  }
  function hideDemoGate() {
    var g = $("demoGate");
    if (g) g.hidden = true;
    document.body.classList.remove("gate-open");
  }

  if (tg) {
    try {
      tg.ready();
      tg.expand();
      if (tg.setHeaderColor) tg.setHeaderColor("#0C1320");
      if (tg.setBackgroundColor) tg.setBackgroundColor("#0C1320");
    } catch (e) { /* старые клиенты */ }
  }

  /* ── Состояние ── */
  var state = {
    cfg: Object.assign({}, DEFAULTS),
    config: Object.assign({}, DEFAULT_CONFIG),
    services: [],
    svc: null,        // текущий сервис
    planId: null,     // выбранный тариф
    orderId: null,    // текущий заказ (checkout / pay / status)
    order: null,      // последний загруженный view заказа
    pay: null,        // данные экрана оплаты
    busy: false,
    stack: [],        // навигационный стек
    pollTimer: null,  // поллинг статуса заказа
    payPoll: null,    // поллинг заказа на экране оплаты
    tickTimer: null   // таймер обратного отсчёта
  };

  /* ── Утилиты ── */
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function usd(v) { return "$" + Number(v).toFixed(2); }
  function rub(v) { return Number(v).toLocaleString("ru-RU") + " ₽"; }
  function haptic(kind) {
    if (!tg || !tg.HapticFeedback) return;
    try { tg.HapticFeedback.impactOccurred(kind || "light"); } catch (e) {}
  }
  function notifyEv(kind) {
    if (!tg || !tg.HapticFeedback) return;
    try { tg.HapticFeedback.notificationOccurred(kind || "success"); } catch (e) {}
  }

  var toastTimer = null;
  function toast(msg, ms) {
    var t = $("toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove("show"); }, ms || 2600);
  }

  /* fetch с таймаутом и JSON */
  function api(path, opts) {
    opts = opts || {};
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, 15000);
    opts.signal = ctl.signal;
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    return fetch(path, opts).then(function (r) {
      clearTimeout(timer);
      return r.json().catch(function () { return { ok: false, error: "Некорректный ответ сервера" }; });
    }).finally(function () { clearTimeout(timer); });
  }

  function copyText(text, okMsg) {
    var done = function () { haptic("light"); toast(okMsg || "Скопировано"); };
    var fallback = function () {
      try {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
        done();
      } catch (e) { toast("Не удалось скопировать"); }
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  }

  function openLink(url) {
    if (!url) return;
    if (tg && tg.openLink) tg.openLink(url);
    else window.open(url, "_blank");
  }

  /* Сервер отдаёт naive-UTC ISO (utcnow().isoformat()) — JS парсит
     такую строку как ЛОКАЛЬНОЕ время. Добавляем 'Z', чтобы таймер
     не уезжал на величину часового пояса устройства. */
  function parseServerDate(s) {
    var v = String(s || "");
    if (!v) return NaN;
    if (/^\d{4}-\d{2}-\d{2}T[\d:.]+$/.test(v)) v += "Z";
    return new Date(v).getTime();
  }

  /* confirm() в WebView Telegram может молча не показаться —
     используем нативный tg.showConfirm с фоллбэком. */
  function askConfirm(question, cb) {
    if (tg && tg.showConfirm) {
      try { tg.showConfirm(question, cb); return; } catch (e) { /* фоллбэк */ }
    }
    cb(window.confirm(question) ? "ok" : "cancelled");
  }

  function clearTimers() {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
    if (state.payPoll) { clearInterval(state.payPoll); state.payPoll = null; }
    if (state.tickTimer) { clearInterval(state.tickTimer); state.tickTimer = null; }
  }

  /* ── Навигация между экранами (со стеком для кнопки «Назад») ── */
  var RELOADERS = {};  // screenId → fn() при возврате назад

  function show(screenId, push) {
    clearTimers();
    var screens = document.querySelectorAll(".screen");
    for (var i = 0; i < screens.length; i++) screens[i].classList.remove("screen-active");
    var scr = $(screenId);
    /* v26: направленный переход — вперёд слайдит вправо, назад влево */
    scr.setAttribute("data-dir", push === false ? "back" : "fwd");
    scr.classList.add("screen-active");
    window.scrollTo(0, 0);

    if (push !== false) {
      if (state.stack[state.stack.length - 1] !== screenId) state.stack.push(screenId);
    }
    if (tg && tg.BackButton) {
      if (state.stack.length > 1) tg.BackButton.show();
      else tg.BackButton.hide();
    }
    $("orderBar").classList.toggle("visible", screenId === "screen-service");
    /* v26: подсветка активной кнопки навигации в шапке */
    $("navOrders").classList.toggle("active", screenId === "screen-orders");
    $("navProfile").classList.toggle("active", screenId === "screen-profile");
    if (!DEMO) $("demoBadge").hidden = true;
    else $("demoBadge").hidden = (screenId !== "screen-catalog" && screenId !== "screen-service");
  }

  /* ── v26: анимированный счётчик чисел (цены, статистика) ── */
  function countUp(el, to, fmt, ms) {
    if (!el) return;
    var from = parseFloat(el.getAttribute("data-v") || "0") || 0;
    el.setAttribute("data-v", to);
    if (Math.abs(to - from) < 0.005) { el.textContent = fmt(to); return; }
    var t0 = (window.performance && performance.now) ? performance.now() : Date.now();
    var dur = ms || 650;
    function frame(t) {
      var k = Math.min(1, ((t || Date.now()) - t0) / dur);
      var e = 1 - Math.pow(1 - k, 3); /* easeOutCubic */
      el.textContent = fmt(from + (to - from) * e);
      if (k < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  /* ── v26: каскадная задержка появления для списков ── */
  function revealSeq(list, base, step) {
    if (!list) return;
    for (var i = 0; i < list.children.length; i++) {
      list.children[i].classList.add("reveal");
      list.children[i].style.setProperty("--rd", (base || 0) + i * (step || 55) + "ms");
    }
  }

  /* ── v26: конфетти при активации подписки ── */
  var CONFETTI_COLORS = ["#1EAEFB", "#378EE5", "#26A17B", "#F5C64D", "#FF6B81", "#B388FF", "#7CE7C4"];
  var lastConfettiOrder = null;
  function confettiBurst() {
    var cx = window.innerWidth / 2, cy = window.innerHeight * 0.3;
    for (var i = 0; i < 26; i++) {
      var p = document.createElement("i");
      p.className = "confetti-bit";
      var ang = Math.random() * Math.PI * 2;
      var dist = 60 + Math.random() * 140;
      p.style.left = cx + "px";
      p.style.top = cy + "px";
      p.style.background = CONFETTI_COLORS[i % CONFETTI_COLORS.length];
      p.style.setProperty("--cdx", Math.round(Math.cos(ang) * dist) + "px");
      p.style.setProperty("--cdy", Math.round(Math.abs(Math.sin(ang)) * 120 + 70) + "px");
      p.style.setProperty("--cdr", Math.round(Math.random() * 720 - 360) + "deg");
      p.style.setProperty("--cd", (1.15 + Math.random() * 0.85) + "s");
      p.style.setProperty("--cdl", (Math.random() * 0.14) + "s");
      p.style.borderRadius = Math.random() > 0.5 ? "50%" : "2px";
      document.body.appendChild(p);
      (function (n) { setTimeout(function () { if (n.parentNode) n.parentNode.removeChild(n); }, 2500); })(p);
    }
  }

  /* ── v26: ripple-отклик на касание кнопок и карточек ── */
  if (document.documentElement.addEventListener) {
    document.addEventListener("pointerdown", function (e) {
      if (!e.target || !e.target.closest) return;
      var t = e.target.closest(".svc, .method, .plan, .order-row, .btn, .icon-btn, .kv-copy, .ref-btn");
      if (!t) return;
      var r = t.getBoundingClientRect();
      var d = Math.max(r.width, r.height) * 1.15;
      var s = document.createElement("span");
      s.className = "ripple";
      s.style.width = s.style.height = d + "px";
      s.style.left = (e.clientX - r.left) + "px";
      s.style.top = (e.clientY - r.top) + "px";
      t.appendChild(s);
      setTimeout(function () { if (s.parentNode) s.parentNode.removeChild(s); }, 650);
    }, { passive: true });
  }

  function back() {
    haptic("light");
    if (state.stack.length > 1) {
      state.stack.pop();
      var prev = state.stack[state.stack.length - 1];
      var rel = RELOADERS[prev];
      if (rel) rel();
      else show(prev, false);
    } else {
      show("screen-catalog", false);
    }
  }

  if (tg && tg.BackButton) {
    tg.BackButton.onClick(function () { back(); });
  }

  /* ── Рендер каталога ── */
  function renderCatalog() {
    var list = $("svcList");
    list.innerHTML = "";
    var n = state.services.length;

    state.services.forEach(function (svc) {
      var b = brandOf(svc);
      var min = Infinity;
      (svc.plans || []).forEach(function (p) { if (p.price_usdt < min) min = p.price_usdt; });
      var el = document.createElement("button");
      el.className = "svc";
      el.style.setProperty("--brand", b.color);
      el.style.setProperty("--brand-soft", b.soft);
      el.style.setProperty("--brand-line", b.line);
      el.style.setProperty("--ga", b.art.a);
      el.style.setProperty("--gb", b.art.b);
      el.innerHTML =
        '<div class="svc-icon">' + svcIconHTML(svc) + "</div>" +
        '<div class="svc-info">' +
          '<div class="svc-name">' + esc(svc.name) + "</div>" +
          '<div class="svc-meta">' + (svc.plans || []).length + " тарифа · от <b>" + usd(min) + "</b></div>" +
        "</div>" +
        '<div class="svc-chev">' + iconTag("chev") + '</div>';
      el.addEventListener("click", function () { haptic("light"); openService(svc); });
      list.appendChild(el);
    });
    revealSeq(list, 80, 60);

    $("catalogCount").textContent = n + " сервисов";
    countUp($("statServices"), n, function (v) { return String(Math.round(v)); });

    var minAll = Infinity;
    state.services.forEach(function (s) {
      (s.plans || []).forEach(function (p) { if (p.price_usdt < minAll) minAll = p.price_usdt; });
    });
    if (isFinite(minAll)) countUp($("statFrom"), minAll, usd, 750);
  }

  /* ── Экран сервиса ── */
  function openService(svc) {
    state.svc = svc;
    state.planId = null;
    var b = brandOf(svc);

    var hero = $("svcHero");
    hero.style.setProperty("--brand", b.color);
    hero.style.setProperty("--brand-soft", b.soft);
    hero.style.setProperty("--brand-line", b.line);
    hero.style.setProperty("--ga", b.art.a);
    hero.style.setProperty("--gb", b.art.b);
    $("svcHeroIcon").innerHTML = svcIconHTML(svc);
    $("svcHeroName").textContent = svc.name;
    $("svcHeroDesc").textContent = svc.description || "";

    var list = $("planList");
    list.innerHTML = "";
    (svc.plans || []).forEach(function (p, i) {
      var perDay = p.price_usdt / Math.max(1, p.duration_days);
      var el = document.createElement("button");
      el.className = "plan" + (i === 0 ? " plan-selected" : "");
      el.setAttribute("data-plan", p.id);
      el.innerHTML =
        '<div class="plan-radio"></div>' +
        '<div class="plan-info">' +
          '<div class="plan-name">' + esc(p.name) + "</div>" +
          '<div class="plan-sub">' + p.duration_days + " дн. · ~" + usd(perDay) + "/день</div>" +
        "</div>" +
        '<div class="plan-price">' + usd(p.price_usdt) + "</div>";
      el.addEventListener("click", function () {
        haptic("light");
        state.planId = p.id;
        var rows = list.children;
        for (var k = 0; k < rows.length; k++) rows[k].classList.remove("plan-selected");
        el.classList.add("plan-selected");
        updateTotal();
      });
      list.appendChild(el);
      if (i === 0) state.planId = p.id;
    });
    revealSeq(list, 60, 50);

    updateTotal();
    show("screen-service");
    watchCustomEmoji();
  }

  function selectedPlan() {
    if (!state.svc) return null;
    var plans = state.svc.plans || [];
    for (var i = 0; i < plans.length; i++) if (plans[i].id === state.planId) return plans[i];
    return null;
  }

  function updateTotal() {
    var p = selectedPlan();
    /* v26: цена меняется с анимированным счётчиком */
    countUp($("orderTotal"), p ? p.price_usdt : 0, usd, 420);
  }

  /* ── Оформление заказа: создание ── */
  function placeOrder() {
    if (state.busy) return;
    var plan = selectedPlan();
    if (!state.svc || !plan) { toast("Выберите тариф"); return; }
    haptic("medium");

    if (DEMO) { showDemoGate(); return; }

    state.busy = true;
    var btn = $("btnOrder");
    btn.disabled = true;
    btn.classList.add("btn-loading"); /* v26: спиннер вместо текста */

    api("/api/order", {
      method: "POST",
      body: JSON.stringify({
        initData: tg.initData,
        service_id: state.svc.id,
        plan_id: plan.id
      })
    }).then(function (res) {
      if (!res || !res.ok) {
        toast((res && res.error) || "Не удалось создать заказ. Попробуйте ещё раз.");
        return;
      }
      notifyEv();
      goCheckout(res.order_id);
    }).catch(function () {
      toast("Нет связи с сервером. Проверьте интернет и повторите.");
    }).finally(function () {
      state.busy = false;
      btn.disabled = false;
      btn.classList.remove("btn-loading");
      btn.textContent = "Оформить заказ";
    });
  }

  /* ── Экран оформления: заказ + промокод + способы оплаты ── */
  function loadOrderView(orderId, cb) {
    return api("/api/order/" + orderId + "/info", {
      method: "POST",
      body: JSON.stringify({ initData: tg.initData })
    }).then(function (res) {
      if (res && res.ok && res.order) {
        state.order = res.order;
        state.orderId = res.order.order_id;
        if (cb) cb(res.order);
        return res.order;
      }
      toast((res && res.error) || "Заказ не найден");
      return null;
    }).catch(function () {
      toast("Нет связи с сервером");
      return null;
    });
  }

  function goCheckout(orderId) {
    loadOrderView(orderId, function (o) {
      if (!o) return;
      if (o.status !== "pending_payment") { goStatus(orderId); return; }
      renderCheckout(o);
      show("screen-checkout");
    });
  }
  RELOADERS["screen-checkout"] = function () {
    if (state.orderId) goCheckout(state.orderId); else back();
  };

  function renderCheckout(o) {
    var rows = "";
    rows += '<div class="oc-head">' +
      '<div class="oc-head-name">' + esc(o.service_name) + "</div>" +
      '<div class="oc-head-plan">' + esc(o.plan_name) + " · " + o.duration_days + " дн." + "</div></div>";

    var hasOrig = o.original_price_usdt && o.original_price_usdt > o.price_usdt;
    if (hasOrig) {
      rows += '<div class="oc-row"><span>Стоимость</span><b><span class="oc-dash">' +
        usd(o.original_price_usdt) + "</span>" + usd(o.price_usdt) + "</b></div>";
      rows += '<div class="oc-row"><span>Скидка</span><b style="color:var(--green)">−' +
        o.discount_pct + "%</b></div>";
    }
    if (o.promo_code) {
      rows += '<div class="oc-row"><span>Промокод</span><b>' + esc(o.promo_code) + "</b></div>";
    }
    if (o.bonus_applied > 0) {
      rows += '<div class="oc-row"><span>Бонус за друзей</span><b style="color:var(--green)">−' +
        usd(o.bonus_applied) + "</b></div>";
    }
    rows += '<div class="oc-row oc-total"><span>К оплате</span><b>' + usd(o.price_usdt) + "</b></div>";
    rows += '<div class="oc-note">После оплаты введите данные аккаунта — активация обычно занимает 5–15 минут.</div>';
    $("coCard").innerHTML = rows;

    // Промокод
    var promoBox = $("promoBox");
    if (state.config.promo_enabled) {
      promoBox.style.display = "";
      $("promoInput").value = o.promo_code || "";
      setPromoHint(o.promo_code ? "Промокод применён — скидка уже в цене" : "", "");
    } else {
      promoBox.style.display = "none";
    }

    // Способы оплаты
    var list = $("methodList");
    list.innerHTML = "";
    $("coHint").textContent = "Оплата внутри Telegram";
    var methods = state.config.methods || [];
    methods.forEach(function (m) {
      var art = METHOD_ART[m];
      if (!art) return;
      var sub = art.sub;
      if (m === "card") {
        var prov = state.config.card_provider;
        if (!prov) return;  // карты отключены
        sub = CARD_SUB[prov] || sub;
      }
      if ((m === "ton" || m === "usdt") && !state.config.wallet_configured) return;

      var el = document.createElement("button");
      el.className = "method";
      el.style.setProperty("--ma", art.a);
      el.style.setProperty("--mb", art.b);
      el.style.setProperty("--m", hexA(art.a, .5));
      el.innerHTML =
        '<div class="method-ico">' + methodIconHTML(m) + "</div>" +
        '<div class="method-info">' +
          '<div class="method-name">' + esc(art.name) + "</div>" +
          '<div class="method-sub">' + esc(sub) + "</div>" +
        "</div>" +
        '<div class="method-chev">' + iconTag("chev") + "</div>";
      el.addEventListener("click", function () { haptic("light"); chooseMethod(m); });
      list.appendChild(el);
    });
    revealSeq(list, 40, 45);
    watchCustomEmoji();
  }

  function setPromoHint(text, cls) {
    var h = $("promoHint");
    h.textContent = text || "";
    h.className = "promo-hint" + (cls ? " " + cls : "");
  }

  /* v26: визуальный фидбек промокода — зелёная вспышка или встряхивание */
  function promoFeedback(ok) {
    var box = $("promoBox");
    box.classList.remove("flash-ok", "shake");
    /* reflow, чтобы анимация перезапустилась при повторе */
    void box.offsetWidth;
    box.classList.add(ok ? "flash-ok" : "shake");
    if (ok) setTimeout(function () { box.classList.remove("flash-ok"); }, 1400);
  }

  function applyPromo() {
    if (state.busy || !state.orderId || DEMO) return;
    var code = $("promoInput").value.trim();
    if (!code) { setPromoHint("Введите промокод", "bad"); return; }
    state.busy = true;
    var btn = $("promoApply");
    btn.disabled = true;
    btn.classList.add("btn-loading"); /* v26 */

    api("/api/order/" + state.orderId + "/promo", {
      method: "POST",
      body: JSON.stringify({ initData: tg.initData, code: code })
    }).then(function (res) {
      if (res && res.ok) {
        notifyEv();
        setPromoHint(res.message || "Промокод применён", "ok");
        promoFeedback(true);
        if (res.order) { state.order = res.order; renderCheckout(res.order); }
      } else {
        setPromoHint((res && res.error) || "Промокод недействителен", "bad");
        promoFeedback(false);
        haptic("heavy");
      }
    }).catch(function () {
      setPromoHint("Нет связи с сервером", "bad");
      promoFeedback(false);
    }).finally(function () {
      state.busy = false;
      btn.disabled = false;
      btn.classList.remove("btn-loading");
    });
  }

  /* ── Выбор способа оплаты ── */
  function chooseMethod(m) {
    if (m === "card" && state.config.card_provider === "digiseller") {
      renderPayEmail();
      show("screen-pay");
      return;
    }
    startPay(m, {});
  }

  function startPay(method, extra) {
    if (state.busy || DEMO) return;
    state.busy = true;
    api("/api/order/" + state.orderId + "/pay", {
      method: "POST",
      body: JSON.stringify(Object.assign({ initData: tg.initData, method: method }, extra || {}))
    }).then(function (res) {
      if (!res || !res.ok) {
        toast((res && res.error) || "Не удалось начать оплату");
        return;
      }
      state.pay = res;
      if (res.method === "stars") {
        renderPayStars(res);
        show("screen-pay");
        openStarsInvoice(res.invoice_link);
      } else {
        renderPay(res);
        show("screen-pay");
      }
    }).catch(function () {
      toast("Нет связи с сервером");
    }).finally(function () { state.busy = false; });
  }

  /* ── Экран оплаты ── */
  function payHead(methodKey, title, amountHtml) {
    var art = METHOD_ART[methodKey] || METHOD_ART.stars;
    var head = $("payHead");
    head.style.setProperty("--pa", art.a);
    head.style.setProperty("--pb", art.b);
    head.style.setProperty("--pa-soft", hexA(art.a, .13));
    head.style.setProperty("--pa-line", hexA(art.a, .38));
    head.innerHTML =
      '<div class="pay-head-ico">' + methodIconHTML(methodKey) + "</div>" +
      '<div class="pay-head-info">' +
        '<div class="pay-head-name">' + esc(title) + "</div>" +
        '<div class="pay-head-amount">' + amountHtml + "</div>" +
      "</div>";
    watchCustomEmoji();
  }

  function kvRow(label, value, copyable) {
    return '<div class="kv">' +
      '<div class="kv-main"><div class="kv-label">' + esc(label) + '</div>' +
      '<div class="kv-value">' + esc(value) + "</div></div>" +
      (copyable ? '<button class="kv-copy" data-copy="' + esc(value) + '" aria-label="Скопировать">' +
        iconTag("copy") + "</button>" : "") +
      "</div>";
  }

  function bindCopyButtons(root) {
    var btns = (root || document).querySelectorAll(".kv-copy[data-copy]");
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener("click", function () {
        copyText(this.getAttribute("data-copy"));
      });
    }
  }

  function startPayTimer(expiresAt) {
    var el = $("payTimer"), txt = $("payTimerText");
    clearInterval(state.tickTimer);
    if (!expiresAt) { el.hidden = true; return; }
    var end = parseServerDate(expiresAt);
    if (isNaN(end)) { el.hidden = true; return; }
    el.hidden = false;
    var tick = function () {
      var left = Math.floor((end - Date.now()) / 1000);
      if (left <= 0) {
        clearInterval(state.tickTimer);
        state.tickTimer = null;
        txt.textContent = "Время оплаты истекло";
        toast("Время оплаты истекло — оформите заказ заново", 3400);
        setTimeout(function () { goStatus(state.orderId); }, 1200);
        return;
      }
      var mm = Math.floor(left / 60), ss = left % 60;
      txt.textContent = "Оплата действительна: " + mm + ":" + (ss < 10 ? "0" : "") + ss;
    };
    tick();
    state.tickTimer = setInterval(tick, 1000);
  }

  function payActions(arr) {
    var box = $("payActions");
    box.innerHTML = "";
    arr.forEach(function (a) {
      var b = document.createElement("button");
      b.className = "btn btn-block " + (a.cls || "btn-ghost");
      if (a.act) b.setAttribute("data-act", a.act); /* v26: поиск кнопки для спиннера */
      b.innerHTML = (a.icon ? iconTag(a.icon) : "") + "<span>" + esc(a.label) + "</span>";
      b.addEventListener("click", a.onClick);
      box.appendChild(b);
    });
  }

  /* TON / USDT / Tribute — общий вид экрана оплаты */
  function renderPay(p) {
    var o = state.order || {};
    var amountHtml;
    if (p.method === "ton") {
      payHead("ton", "Оплата Gram", "Сумма: <b>" + p.amount_ton.toFixed(3) + " Gram</b>" +
        (p.rub_price ? " · " + rub(p.rub_price) : ""));
      amountHtml = null;
      $("payBody").innerHTML =
        kvRow("Кошелёк для оплаты", p.address, true) +
        kvRow("Комментарий (обязательно)", p.memo, true) +
        '<div class="pay-warn">' + iconTag("info") + "<div>Отправляйте <b>точно</b> указанную сумму Gram и обязательно укажите комментарий " +
          "<b>" + esc(p.memo) + "</b> — без него мы не сможем зачесть оплату.</div></div>" +
        '<div class="pay-note">' + iconTag("shield") + "<div>Оплата подтверждается автоматически, обычно за 1–2 минуты. Кнопка «Проверить оплату» — если не хотите ждать.</div></div>";
    } else if (p.method === "usdt") {
      payHead("usdt", "Оплата USDT", "Сумма: <b>" + p.amount_usdt.toFixed(2) + " USDT</b>" +
        (p.rub_price ? " · " + rub(p.rub_price) : ""));
      $("payBody").innerHTML =
        kvRow("Кошелёк (сеть Gram)", p.address, true) +
        kvRow("Комментарий (обязательно)", p.memo, true) +
        '<div class="pay-warn">' + iconTag("info") + "<div>Отправляйте USDT <b>только в сети Gram</b> и обязательно укажите комментарий " +
          "<b>" + esc(p.memo) + "</b> — без него мы не сможем зачесть оплату.</div></div>" +
        '<div class="pay-note">' + iconTag("shield") + "<div>Оплата подтверждается автоматически, обычно за 1–2 минуты.</div></div>";
    } else if (p.method === "card" && p.provider === "tribute") {
      payHead("card", "Оплата картой", "Сумма: <b>" + rub(p.rub_price) + "</b>" +
        (p.price_usdt ? " · " + usd(p.price_usdt) : ""));
      $("payBody").innerHTML =
        '<div class="pay-note">' + iconTag("bolt") + "<div>Оплата откроется прямо в Telegram — карты, СБП и другие способы. После оплаты подписка подтверждается автоматически в течение минуты.</div></div>" +
        '<div class="pay-note">' + iconTag("clock") + "<div>Оплата действительна 60 минут после перехода на страницу оплаты.</div></div>";
    } else {
      // Digiseller после создания ссылки
      payHead("card", "Оплата картой", "Сумма: <b>" + rub(p.rub_price) + "</b>" +
        (p.price_usdt ? " · " + usd(p.price_usdt) : ""));
      $("payBody").innerHTML =
        '<div class="pay-note">' + iconTag("bolt") + "<div>Перейдите по кнопке ниже и оплатите картой. Чек придёт на указанный email.</div></div>" +
        '<div class="pay-note">' + iconTag("clock") + "<div>Оплата действительна 60 минут после перехода на страницу оплаты.</div></div>";
    }

    startPayTimer(p.expires_at);

    var acts = [];
    if (p.method === "ton" && p.pay_url) {
      acts.push({ label: "Открыть Tonkeeper", cls: "btn-primary", icon: "wallet", onClick: function () { haptic("medium"); openLink(p.pay_url); } });
      if (p.ton_url) acts.push({ label: "Открыть Gram Wallet", icon: "arrow-out", onClick: function () { haptic("medium"); openLink(p.ton_url); } });
    }
    if (p.method === "usdt" && p.pay_url) {
      acts.push({ label: "Отправить USDT", cls: "btn-primary", icon: "wallet", onClick: function () { haptic("medium"); openLink(p.pay_url); } });
    }
    if (p.method === "card") {
      acts.push({ label: "Оплатить картой", cls: "btn-primary", icon: "card", onClick: function () { haptic("medium"); openLink(p.pay_url); } });
    }
    acts.push({ label: "Проверить оплату", cls: "btn-ok", icon: "refresh", act: "check", onClick: checkPayment });
    acts.push({ label: "Отменить заказ", cls: "btn-danger", icon: "close", onClick: cancelOrderFlow });
    payActions(acts);

    // Живой дозор: если фоновый поллер бота подтвердил оплату — сразу на статус
    clearInterval(state.payPoll);
    state.payPoll = setInterval(function () {
      if (!state.orderId) return;
      api("/api/order/" + state.orderId + "/info", {
        method: "POST",
        body: JSON.stringify({ initData: tg.initData })
      }).then(function (res) {
        if (res && res.ok && res.order && res.order.status !== "pending_payment") {
          clearInterval(state.payPoll);
          state.payPoll = null;
          notifyEv();
          goStatus(state.orderId);
        }
      }).catch(function () {});
    }, 5000);
  }

  /* Digiseller: шаг ввода email */
  function renderPayEmail() {
    payHead("card", "Оплата картой", "Сумма: <b>" + usd((state.order || {}).price_usdt || 0) + "</b>");
    $("payTimer").hidden = true;
    $("payBody").innerHTML =
      '<div class="pay-email">' +
        '<div class="pay-email-label">Ваш email для чека</div>' +
        '<div class="pay-email-sub">Он будет подставлен на страницу оплаты, и по нему мы найдём ваш платёж.</div>' +
        '<input id="digiEmail" class="input" type="email" inputmode="email" placeholder="ivan@mail.ru" autocomplete="email">' +
        '<div class="field-hint">Оплата действительна 60 минут после перехода на страницу.</div>' +
      "</div>";
    payActions([
      { label: "Перейти к оплате", cls: "btn-primary", icon: "card", onClick: function () {
          var email = ($("digiEmail") && $("digiEmail").value || "").trim();
          if (!/^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/.test(email)) {
            toast("Введите корректный email — на него придёт чек");
            return;
          }
          startPay("card", { email: email });
        } },
      { label: "Назад", icon: "chev", onClick: function () { back(); } }
    ]);
  }

  /* Stars */
  function renderPayStars(p) {
    payHead("stars", "Telegram Stars", "Сумма: <b>" + p.stars + " Stars</b>" +
      (p.price_usdt ? " · " + usd(p.price_usdt) : ""));
    $("payTimer").hidden = true;
    $("payBody").innerHTML =
      '<div class="pay-note">' + iconTag("bolt") + "<div>Подтвердите счёт в открывшемся окне Telegram. Оплата зачисляется автоматически в течение секунды.</div></div>";
    payActions([
      { label: "Оплатить снова", cls: "btn-gold", icon: "star", onClick: function () {
          if (p.invoice_link) openStarsInvoice(p.invoice_link);
        } },
      { label: "Проверить оплату", cls: "btn-ok", icon: "refresh", act: "check", onClick: checkPayment }
    ]);
  }

  function openStarsInvoice(link) {
    if (!tg || !tg.openInvoice) {
      toast("Оплата Stars доступна в Telegram");
      return;
    }
    haptic("medium");
    try {
      tg.openInvoice(link, function (status) {
        if (status === "paid") {
          notifyEv();
          toast("Оплата прошла! Подтверждаем…", 2200);
          setTimeout(function () { goStatus(state.orderId); }, 900);
        } else if (status === "failed") {
          toast("Оплата не прошла. Попробуйте ещё раз.");
        } else if (status === "cancelled") {
          toast("Счёт закрыт — можно оплатить позже из «Заказов»");
        } else {
          setTimeout(function () { goStatus(state.orderId); }, 1500);
        }
      });
    } catch (e) {
      setTimeout(function () { goStatus(state.orderId); }, 1500);
    }
  }

  /* ── Проверка оплаты (кнопка) ── */
  function checkPayment() {
    if (state.busy || !state.orderId || DEMO) return;
    state.busy = true;
    /* v26: спиннер прямо на кнопке «Проверить оплату» */
    var btn = document.querySelector('#payActions [data-act="check"]');
    if (btn) btn.classList.add("btn-loading");
    api("/api/order/" + state.orderId + "/check", {
      method: "POST",
      body: JSON.stringify({ initData: tg.initData })
    }).then(function (res) {
      if (!res) { toast("Нет связи с сервером"); return; }
      if (!res.ok) { toast(res.error || "Не удалось проверить оплату"); return; }
      if (res.paid) {
        notifyEv();
        toast(res.message || "Оплата подтверждена!");
        goStatus(state.orderId);
      } else {
        toast(res.message || "Оплата не найдена", 3200);
      }
    }).catch(function () {
      toast("Нет связи с сервером");
    }).finally(function () {
      state.busy = false;
      if (btn) btn.classList.remove("btn-loading");
    });
  }

  /* ── Отмена заказа ── */
  function cancelOrderFlow() {
    haptic("medium");
    if (!state.orderId || DEMO) return;
    askConfirm("Отменить заказ? Оформленный заказ и скидки будут удалены.", function (answer) {
      if (answer !== "ok") return;
      api("/api/order/" + state.orderId + "/cancel", {
        method: "POST",
        body: JSON.stringify({ initData: tg.initData })
      }).then(function (res) {
        if (res && res.ok) {
          notifyEv("warning");
          toast("Заказ отменён");
          goCatalog();
        } else {
          toast((res && res.error) || "Не удалось отменить заказ", 3400);
        }
      }).catch(function () { toast("Нет связи с сервером"); });
    });
  }

  /* ── Экран статуса заказа ── */
  var STATUS_META = {
    pending_payment:  { label: "Ожидает оплату",       cls: "chip-warn", step: 0 },
    payment_over:     { label: "Время оплаты истекло", cls: "chip-bad",  step: 0 },
    pending_account:  { label: "Введите данные",       cls: "chip-info", step: 1 },
    pending_activation: { label: "Активация",          cls: "chip-info", step: 2 },
    active:           { label: "Активна",              cls: "chip-ok",   step: 3 },
    cancelled:        { label: "Отменён",              cls: "chip-bad",  step: -1 },
    failed:           { label: "Ошибка",               cls: "chip-bad",  step: -1 },
    expired:          { label: "Истекла",              cls: "chip-bad",  step: 3 }
  };

  function renderStepper(step) {
    var steps = [
      { icon: "card", label: "Оплата" },
      { icon: "key", label: "Данные" },
      { icon: "clock", label: "Активация" },
      { icon: "checkmark", label: "Готово" }
    ];
    var html = "";
    for (var i = 0; i < steps.length; i++) {
      var cls = "step";
      if (step > i) cls += " step-done";
      else if (step === i) cls += " step-now";
      html += '<div class="' + cls + '">' +
        '<div class="step-line"></div>' +
        '<div class="step-dot">' + (step > i ? iconTag("checkmark") : iconTag(steps[i].icon)) + "</div>" +
        '<div class="step-label">' + steps[i].label + "</div></div>";
    }
    $("stStepper").innerHTML = html;
  }

  function goStatus(orderId) {
    loadOrderView(orderId, function (o) {
      if (!o) return;
      renderStatus(o);
      show("screen-status");
      watchCustomEmoji();
    });
  }
  RELOADERS["screen-status"] = function () {
    if (state.orderId) goStatus(state.orderId); else back();
  };

  function renderStatus(o) {
    $("stTitle").textContent = "Заказ №" + o.order_id;
    var meta = STATUS_META[o.status] || STATUS_META.pending_activation;
    renderStepper(meta.step);

    var rows = '<div class="st-rows">' +
      '<div class="st-row"><span>Подписка</span><b>' + esc(o.service_name) + " — " + esc(o.plan_name) + "</b></div>" +
      '<div class="st-row"><span>Срок</span><b>' + o.duration_days + " дн.</b></div>" +
      '<div class="st-row"><span>К оплате</span><b>' + usd(o.price_usdt) +
        (o.discount_pct > 0 ? " · −" + o.discount_pct + "%" : "") + "</b></div>" +
      (o.note ? '<div class="st-row"><span>Действует</span><b>' + esc(o.note) + "</b></div>" : "") +
      "</div>";

    var body = "";
    var acts = [];

    if (o.status === "pending_payment") {
      body =
        '<div class="st-card"><div class="st-head"><div class="st-ico warn">' + iconTag("clock") + "</div>" +
        '<div><div class="st-title">Ожидает оплату</div>' +
        '<div class="st-sub">Выберите способ оплаты — всё делается прямо здесь, без переписки.</div></div></div>' +
        rows + "</div>";
      acts.push({ label: "Перейти к оплате", cls: "btn-primary", icon: "card", onClick: function () { goCheckout(o.order_id); } });
      acts.push({ label: "Отменить заказ", cls: "btn-danger", icon: "close", onClick: cancelOrderFlow });
    } else if (o.status === "payment_over") {
      body =
        '<div class="st-card"><div class="st-head"><div class="st-ico bad">' + iconTag("clock") + "</div>" +
        '<div><div class="st-title">Время оплаты истекло</div>' +
        '<div class="st-sub">Если вы уже отправили деньги — напишите в поддержку, платёж проверят вручную.</div></div></div>' +
        rows + "</div>";
      acts.push({ label: "Оформить заново", cls: "btn-primary", icon: "refresh", onClick: goCatalog });
      acts.push({ label: "Написать в поддержку", icon: "chat", onClick: openSupport });
    } else if (o.status === "pending_account") {
      body =
        '<div class="st-card"><div class="st-head"><div class="st-ico info">' + iconTag("key") + "</div>" +
        '<div><div class="st-title">Оплата подтверждена!</div>' +
        '<div class="st-sub">Введите данные аккаунта, на котором активировать подписку.</div></div></div>' +
        renderAccountForm(o) + "</div>";
    } else if (o.status === "pending_activation") {
      body =
        '<div class="st-card"><div class="st-head"><div class="st-ico info">' + iconTag("clock") + "</div>" +
        '<div><div class="st-title">Ждём активации</div>' +
        '<div class="st-sub">Данные получены — команда активирует подписку. Обычно это 5–15 минут, максимум несколько часов.</div></div></div>' +
        rows + "</div>";
      acts.push({ label: "Написать в поддержку", icon: "chat", onClick: openSupport });
    } else if (o.status === "active") {
      body =
        '<div class="st-card" style="text-align:center">' +
        '<svg class="big-check" viewBox="0 0 72 72" fill="none">' +
          '<circle class="ck-circle" cx="36" cy="36" r="32" stroke="url(#ckg)" stroke-width="3"/>' +
          '<path class="ck-path" d="M22 37l10 10 18-20" stroke="url(#ckg)" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>' +
          "<defs><linearGradient id=\"ckg\" x1=\"4\" y1=\"4\" x2=\"68\" y2=\"68\"><stop stop-color=\"#1EAEFB\"/><stop offset=\"1\" stop-color=\"#26A17B\"/></linearGradient></defs>" +
        "</svg>" +
        '<div class="st-title">Подписка активна!</div>' +
        (o.note ? '<div class="st-sub">Действует ' + esc(o.note) + "</div>" : "") +
        rows + "</div>";
      acts.push({ label: "Продлить подписку", cls: "btn-primary", icon: "sparkle", onClick: function () { renewOrder(o); } });
    } else if (o.status === "expired") {
      body =
        '<div class="st-card"><div class="st-head"><div class="st-ico warn">' + iconTag("clock") + "</div>" +
        '<div><div class="st-title">Подписка закончилась</div>' +
        '<div class="st-sub">Продлите, чтобы не терять доступ — все тарифы на месте.</div></div></div>' +
        rows + "</div>";
      acts.push({ label: "Продлить подписку", cls: "btn-primary", icon: "sparkle", onClick: function () { renewOrder(o); } });
    } else {  // cancelled / failed
      body =
        '<div class="st-card"><div class="st-head"><div class="st-ico bad">' + iconTag("close") + "</div>" +
        '<div><div class="st-title">' + esc(meta.label) + "</div>" +
        '<div class="st-sub">Вы всегда можете оформить новый заказ через каталог.</div></div></div>' +
        "</div>";
      acts.push({ label: "В каталог", cls: "btn-primary", icon: "chev", onClick: goCatalog });
    }

    $("stBody").innerHTML = body;
    var stCard = $("stBody").firstElementChild;
    if (stCard) stCard.classList.add("reveal"); /* v26 */
    /* v26: конфетти при переходе в «Активна» (один раз на заказ) */
    if (o.status === "active" && lastConfettiOrder !== o.order_id) {
      lastConfettiOrder = o.order_id;
      confettiBurst();
    }
    var box = $("stActions");
    box.innerHTML = "";
    acts.forEach(function (a) {
      var b = document.createElement("button");
      b.className = "btn btn-block " + (a.cls || "btn-ghost");
      b.innerHTML = (a.icon ? iconTag(a.icon) : "") + "<span>" + esc(a.label) + "</span>";
      b.addEventListener("click", a.onClick);
      box.appendChild(b);
    });

    bindAccountForm(o);

    // Живой поллинг: пока заказ в промежуточном статусе — обновляем экран
    clearInterval(state.pollTimer);
    if (["pending_account", "pending_activation"].indexOf(o.status) !== -1) {
      state.pollTimer = setInterval(function () {
        if (state.stack[state.stack.length - 1] !== "screen-status") return;
        api("/api/order/" + o.order_id + "/info", {
          method: "POST",
          body: JSON.stringify({ initData: tg.initData })
        }).then(function (res) {
          if (res && res.ok && res.order && res.order.status !== state.order.status) {
            state.order = res.order;
            renderStatus(res.order);
            if (res.order.status === "active") notifyEv();
            watchCustomEmoji();
          }
        }).catch(function () {});
      }, 5000);
    }
  }

  function renderAccountForm(o) {
    var fields = o.account_fields || [];
    var html = '<div class="acc-form" id="accForm">';
    fields.forEach(function (f) {
      var type = f.type === "password" ? "password" : (f.type === "email" ? "email" : "text");
      var im = f.type === "email" ? "email" : (f.type === "password" ? "off" : "off");
      html += '<div class="field">' +
        "<label>" + esc(f.label) + "</label>" +
        '<input class="input" data-fid="' + esc(f.id) + '" type="' + type + '" autocomplete="' + im + '" placeholder="' + esc(f.placeholder || "") + '">' +
        (f.placeholder ? '<div class="field-hint">Пример: ' + esc(f.placeholder) + "</div>" : "") +
        "</div>";
    });
    html += '<button class="btn btn-primary btn-block" id="accSubmit">' + iconTag("checkmark") + "<span>Отправить данные</span></button>";
    html += '<div class="field-hint">Данные используются только для активации и удаляются после. Код из письма, если сервис его запросит, попросим в чате бота.</div>';
    html += "</div>";
    return html;
  }

  function bindAccountForm(o) {
    var form = $("accForm");
    if (!form) return;
    var btn = $("accSubmit");
    if (!btn) return;
    btn.addEventListener("click", function () {
      if (state.busy) return;
      var fields = o.account_fields || [];
      var values = {};
      for (var i = 0; i < fields.length; i++) {
        var input = form.querySelector('input[data-fid="' + fields[i].id + '"]');
        var v = input ? input.value.trim() : "";
        if (!v) { toast("Заполните: " + fields[i].label); input && input.focus(); return; }
        values[fields[i].id] = v;
      }
      state.busy = true;
      btn.disabled = true;
      btn.classList.add("btn-loading"); /* v26 */
      api("/api/order/" + o.order_id + "/account", {
        method: "POST",
        body: JSON.stringify({ initData: tg.initData, fields: values })
      }).then(function (res) {
        if (res && res.ok) {
          notifyEv();
          toast("Данные отправлены — активируем подписку");
          goStatus(o.order_id);
        } else {
          toast((res && res.error) || "Не удалось отправить данные");
          btn.disabled = false;
          btn.classList.remove("btn-loading");
        }
      }).catch(function () {
        toast("Нет связи с сервером");
        btn.disabled = false;
        btn.classList.remove("btn-loading");
      }).finally(function () { state.busy = false; });
    });
  }

  function renewOrder(o) {
    var sid = o.service_id;
    for (var i = 0; i < state.services.length; i++) {
      if (state.services[i].id === sid) { openService(state.services[i]); return; }
    }
    goCatalog();
  }

  function goCatalog() {
    state.stack = ["screen-catalog"];
    show("screen-catalog", false);
  }

  function openSupport() {
    openLink("https://t.me/" + state.cfg.bot_username);
  }

  /* ── Мои заказы ── */
  function goOrders() {
    if (DEMO) { showDemoGate(); return; }
    $("ordersList").innerHTML = '<div class="orders-empty"><div class="skel skel-line w60" style="margin:0 auto"></div></div>';
    show("screen-orders");
    api("/api/orders", {
      method: "POST",
      body: JSON.stringify({ initData: tg.initData, limit: 20 })
    }).then(function (res) {
      if (!res || !res.ok) {
        $("ordersList").innerHTML = '<div class="orders-empty">' + iconTag("info") + "Не удалось загрузить заказы</div>";
        return;
      }
      var list = res.orders || [];
      if (!list.length) {
        $("ordersList").innerHTML =
          '<div class="orders-empty">' + iconTag("box") +
          "У вас пока нет заказов.<br>Оформите подписку через каталог!</div>";
        return;
      }
      renderOrders(list);
    }).catch(function () {
      $("ordersList").innerHTML = '<div class="orders-empty">' + iconTag("info") + "Нет связи с сервером</div>";
    });
  }
  RELOADERS["screen-orders"] = goOrders;

  function renderOrders(list) {
    var box = $("ordersList");
    box.innerHTML = "";
    list.forEach(function (o) {
      var meta = STATUS_META[o.status] || STATUS_META.pending_activation;
      var el = document.createElement("button");
      el.className = "order-row";
      el.innerHTML =
        '<div class="order-row-main">' +
          '<div class="order-row-id">№' + o.order_id + " · " + esc(o.created_at || "") + "</div>" +
          '<div class="order-row-name">' + esc(o.service_name) + " — " + esc(o.plan_name) + "</div>" +
          '<div class="order-row-meta">' + o.duration_days + " дн." +
            (o.note ? " · " + esc(o.note) : "") + "</div>" +
        "</div>" +
        '<div class="order-row-side">' +
          '<div class="order-row-price">' + usd(o.price_usdt) + "</div>" +
          '<span class="chip ' + meta.cls + '">' + esc(meta.label) + "</span>" +
        "</div>";
      el.addEventListener("click", function () { haptic("light"); goStatus(o.order_id); });
      box.appendChild(el);
    });
    revealSeq(box, 30, 50);
  }

  /* ── Профиль: скидка, бонус, рефералка, поддержка ── */
  function goProfile() {
    if (DEMO) { showDemoGate(); return; }
    $("profileBody").innerHTML = "";
    show("screen-profile");
    api("/api/profile", {
      method: "POST",
      body: JSON.stringify({ initData: tg.initData })
    }).then(function (res) {
      if (!res || !res.ok) {
        $("profileBody").innerHTML = '<div class="orders-empty">' + iconTag("info") + "Не удалось загрузить профиль</div>";
        return;
      }
      renderProfile(res);
    }).catch(function () {
      $("profileBody").innerHTML = '<div class="orders-empty">' + iconTag("info") + "Нет связи с сервером</div>";
    });
  }
  RELOADERS["screen-profile"] = goProfile;

  function renderProfile(p) {
    var html = "";

    if (p.welcome_discount && p.welcome_discount.discount_pct > 0) {
      html += '<div class="prof-banner reveal" style="--rd:0ms">' +
        '<div class="prof-banner-ico">' + iconTag("sparkle") + "</div>" +
        "<div><b>Скидка " + p.welcome_discount.discount_pct + "% на первый заказ!</b>" +
        "<span>Применится автоматически при оформлении</span></div></div>";
    }

    html += '<div class="prof-card reveal" style="--rd:60ms">' +
      '<div class="prof-card-head">' + iconTag("wallet") + "<b>Бонусный счёт</b></div>" +
      '<div class="bonus-num">' + p.bonus_balance.toFixed(2) + "<small>USDT</small></div>" +
      '<div class="bonus-sub">Бонусы автоматически вычитаются из стоимости следующего заказа' +
      (p.bonus_reserved > 0 ? ". Сейчас зарезервировано живыми заказами: " + p.bonus_reserved.toFixed(2) + " USDT" : "") +
      ".</div></div>";

    var ref = p.referral || {};
    if (ref.link) {
      var shareUrl = "https://t.me/share/url?url=" + encodeURIComponent(ref.link) +
        "&text=" + encodeURIComponent("Дешёвые подписки в этом боте — заходи!");
      html += '<div class="prof-card reveal" style="--rd:120ms">' +
        '<div class="prof-card-head">' + iconTag("gift") + "<b>Пригласи друга — получи бонус</b></div>" +
        '<div class="bonus-sub">Друг оплачивает подписку по твоей ссылке — тебе начисляется <b>' +
          (ref.bonus_per_friend || 0.5).toFixed(2) + ' USDT</b> бонусом.</div>' +
        '<div class="ref-link"><span>' + esc(ref.link) + "</span>" +
          '<button class="ref-btn" id="refCopy">' + iconTag("copy") + "Копировать</button>" +
          '<button class="ref-btn" id="refShare">' + iconTag("share") + "Поделиться</button></div>" +
        '<div class="ref-stats">' +
          '<div class="ref-stat"><b>' + (ref.invited || 0) + "</b><span>приглашено друзей</span></div>" +
          '<div class="ref-stat"><b>' + (ref.earned || 0).toFixed(2) + "</b><span>заработано, USDT</span></div>" +
        "</div></div>";
      html += '<script type="application/json" id="refData">' + JSON.stringify({ link: ref.link, share: shareUrl }) + "<\/script>";
    }

    var actions = document.createElement("div");
    actions.className = "prof-actions reveal";
    actions.style.setProperty("--rd", "180ms");
    var acts = [
      { label: "Мои заказы", cls: "btn-ghost", icon: "box", onClick: function () { goOrders(); } },
      { label: "Написать в поддержку", cls: "btn-ghost", icon: "chat", onClick: openSupport }
    ];
    acts.forEach(function (a) {
      var b = document.createElement("button");
      b.className = "btn btn-block " + a.cls;
      b.innerHTML = (a.icon ? iconTag(a.icon) : "") + "<span>" + esc(a.label) + "</span>";
      b.addEventListener("click", a.onClick);
      actions.appendChild(b);
    });

    $("profileBody").innerHTML = html;
    $("profileBody").appendChild(actions);

    var refCopy = $("refCopy"), refShare = $("refShare"), refData = $("refData");
    if (refCopy && refData) {
      var d = JSON.parse(refData.textContent);
      refCopy.addEventListener("click", function () { copyText(d.link, "Ссылка скопирована"); });
      refShare.addEventListener("click", function () { haptic("light"); openLink(d.share); });
    }
  }

  /* ── Сессия + каталог ── */
  function loadSession() {
    /* v26: тонкая полоска загрузки, пока тянем каталог */
    document.body.classList.add("is-loading");
    var done = function () { document.body.classList.remove("is-loading"); };
    api("/api/catalog").then(function (res) {
      done();
      if (res && res.ok && Array.isArray(res.services)) {
        state.services = res.services.filter(function (s) { return s.active !== false; });
        /* v27: публичный username бота — для демо-шлюза «Открыть в Telegram» */
        if (res.bot_username) state.cfg.bot_username = res.bot_username;
        if (DEMO) showDemoGate();
        renderCatalog();
        watchCustomEmoji();
        maybeOpenDeepLink();
      } else {
        toast("Каталог временно недоступен");
      }
    }).catch(function () {
      done();
      toast("Не удалось загрузить каталог");
    });

    if (DEMO) {
      $("demoBadge").hidden = false;
      return;
    }
    api("/api/session", {
      method: "POST",
      body: JSON.stringify({ initData: tg.initData })
    }).then(function (res) {
      if (!res || !res.ok) return;
      state.cfg.bot_username = res.bot_username || state.cfg.bot_username;
      state.cfg.store_name = res.store_name || state.cfg.store_name;
      state.cfg.offer_url = res.offer_url || state.cfg.offer_url;
      if (res.config) state.config = Object.assign(state.config, res.config);
      applyCfg();
      if (res.welcome_discount && res.welcome_discount.discount_pct > 0) {
        toast("🎉 Скидка " + res.welcome_discount.discount_pct + "% на первый заказ!", 3400);
      }
    }).catch(function () { /* тихо: бот-фоллбэк всегда работает */ });
  }

  function applyCfg() {
    document.title = state.cfg.store_name + " — цифровые подписки";
    $("storeName").textContent = state.cfg.store_name;
    $("offerLink").href = state.cfg.offer_url;
    $("supportLink").href = "https://t.me/" + state.cfg.bot_username;
  }

  /* ── Deep link: ?startapp=svc_<id> / ord_<id> / orders / profile ── */
  function maybeOpenDeepLink() {
    var sp = null;
    try { sp = tg && tg.initDataUnsafe ? tg.initDataUnsafe.start_param : null; } catch (e) {}
    if (!sp) {
      var m = location.search.match(/[?&]startapp=([a-z0-9_]+)/i);
      if (m) sp = m[1];
    }
    if (!sp) return;

    if (sp.indexOf("svc_") === 0) {
      var sid = sp.slice(4);
      for (var i = 0; i < state.services.length; i++) {
        if (state.services[i].id === sid) { openService(state.services[i]); return; }
      }
      return;
    }
    if (sp.indexOf("ord_") === 0) {
      var oid = parseInt(sp.slice(4), 10);
      if (oid > 0 && !DEMO) goStatus(oid);
      return;
    }
    if (sp === "orders") { goOrders(); return; }
    if (sp === "profile") { goProfile(); }
  }

  /* ── Обработчики ── */
  $("btnScrollCatalog").addEventListener("click", function () {
    haptic("light");
    $("catalogAnchor").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  $("btnHow").addEventListener("click", function () {
    haptic("light");
    $("trustBlock").scrollIntoView({ behavior: "smooth", block: "center" });
    toast("1) Выберите тариф  2) Оплатите в приложении  3) Введите данные аккаунта", 4200);
  });
  $("btnBack").addEventListener("click", back);
  $("btnOrder").addEventListener("click", placeOrder);

  $("coBack").addEventListener("click", back);
  $("promoApply").addEventListener("click", applyPromo);
  $("promoInput").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); applyPromo(); }
  });

  $("payBack").addEventListener("click", back);
  $("stBack").addEventListener("click", back);
  $("ordBack").addEventListener("click", back);
  $("profBack").addEventListener("click", back);

  $("navOrders").addEventListener("click", function () { haptic("light"); goOrders(); });
  $("navProfile").addEventListener("click", function () { haptic("light"); goProfile(); });

  /* v27: демо-шлюз (виден только вне Telegram) */
  var _demoOpen = $("demoGateOpen"), _demoPrev = $("demoGatePreview");
  if (_demoOpen) _demoOpen.addEventListener("click", function () { haptic("light"); });
  if (_demoPrev) _demoPrev.addEventListener("click", function () {
    haptic("light");
    hideDemoGate();
    toast("Превью: заказы и оплата — только внутри Telegram", 2800);
  });

  /* ── Старт ── */
  $("footYear").textContent = new Date().getFullYear();
  applyCfg();
  loadSession();
})();

/* Data-table behaviour for the report tables (table.data-table): click a heading to sort, search box, rows per page.
   The admin lists (Gym members and the rest) have this built in on the server; this gives the report pages that
   show months or ranks across the same feel. Numbers sort as numbers (18,75,475 and 12.5% included). */
(function () {
  var NUMBER = /^[-+]?(\d+(\.\d*)?|\.\d+)$/;   // the whole cell must be a number: "99 Fitness" is text

  function value(cell) {
    var text = (cell.getAttribute("data-sort") || cell.textContent || "").trim();
    var bare = text.replace(/[₹%,\s]/g, "");
    return NUMBER.test(bare) ? parseFloat(bare) : text.toLowerCase();
  }

  function compare(a, b, dir) {
    if (a === "" && b !== "") return 1;           // empty cells always last
    if (b === "" && a !== "") return -1;
    if (typeof a === "number" && typeof b === "number") return (a - b) * dir;
    if (typeof a === "number") return -1;          // numbers before text
    if (typeof b === "number") return 1;
    return a < b ? -dir : a > b ? dir : 0;
  }

  /* One heading per column, for tables whose heading has two levels (colspan / rowspan). */
  function columnHeaders(table) {
    var grid = [], out = [];
    if (!table.tHead) return out;
    Array.prototype.forEach.call(table.tHead.rows, function (row, r) {
      var c = 0;
      grid[r] = grid[r] || [];
      Array.prototype.forEach.call(row.cells, function (cell) {
        while (grid[r][c]) c++;
        for (var dr = 0; dr < (cell.rowSpan || 1); dr++) {
          grid[r + dr] = grid[r + dr] || [];
          for (var dc = 0; dc < (cell.colSpan || 1); dc++) grid[r + dr][c + dc] = cell;
        }
        c += cell.colSpan || 1;
      });
    });
    var last = grid[table.tHead.rows.length - 1] || [];
    for (var i = 0; i < last.length; i++) out.push(last[i]);
    return out;
  }

  function enhance(table) {
    var body = table.tBodies[0];
    if (!body || body.rows.length < 2) return;
    var rows = Array.prototype.slice.call(body.rows);
    var state = { query: "", col: -1, dir: 1, page: 0, size: parseInt(table.getAttribute("data-page-size") || "0", 10) };

    var bar = document.createElement("div");
    bar.className = "data-table-bar";
    var search = document.createElement("input");
    search.type = "search";
    search.placeholder = "Search this table";
    search.setAttribute("aria-label", "Search this table");
    search.className = "border border-base-200 px-3 py-2 rounded-default dark:bg-base-900 dark:border-base-700";
    var sizes = document.createElement("select");
    sizes.className = search.className;
    sizes.setAttribute("aria-label", "Rows per page");
    [[0, "All rows"], [25, "25 per page"], [50, "50 per page"], [100, "100 per page"]].forEach(function (o) {
      var opt = document.createElement("option");
      opt.value = o[0]; opt.textContent = o[1]; opt.selected = o[0] === state.size;
      sizes.appendChild(opt);
    });
    var info = document.createElement("span");
    info.setAttribute("aria-live", "polite");
    var prev = document.createElement("button"), next = document.createElement("button");
    prev.type = next.type = "button";              // a real button: keyboard operable, never submits a form
    prev.textContent = "← Previous"; next.textContent = "Next →";
    prev.className = next.className = "text-primary-600 cursor-pointer";
    [search, sizes, info, prev, next].forEach(function (el) { bar.appendChild(el); });
    var holder = table.closest(".overflow-auto") || table;
    holder.parentNode.insertBefore(bar, holder);

    var heads = columnHeaders(table);   // with grouped (two-level) headings: the bottom-most heading of each column
    heads = heads.map(function (th, i) { return heads.indexOf(th) === i ? th : null; });  // a wide heading sorts by its first column, once
    heads.forEach(function (th, i) {
      if (!th) return;
      function sort() {
        state.dir = state.col === i ? -state.dir : 1;
        state.col = i; state.page = 0; draw();
      }
      th.style.cursor = "pointer";
      th.title = "Click (or Enter) to sort";
      th.tabIndex = 0;
      th.setAttribute("role", "columnheader");
      th.addEventListener("click", sort);
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sort(); }
      });
    });

    function draw() {
      var q = state.query;
      var shown = rows.filter(function (r) { return !q || r.textContent.toLowerCase().indexOf(q) !== -1; });
      if (state.col >= 0) {
        shown.sort(function (a, b) { return compare(value(a.cells[state.col]), value(b.cells[state.col]), state.dir); });
      }
      rows.forEach(function (r) { r.style.display = "none"; });
      var from = state.size ? state.page * state.size : 0;
      var to = state.size ? from + state.size : shown.length;
      shown.forEach(function (r) { body.appendChild(r); });                 // keeps the sorted order
      shown.slice(from, to).forEach(function (r) { r.style.display = ""; });
      info.textContent = shown.length + " of " + rows.length + " rows" +
        (state.size ? " · page " + (state.page + 1) + " of " + Math.max(1, Math.ceil(shown.length / state.size)) : "");
      prev.style.visibility = state.size && state.page > 0 ? "visible" : "hidden";
      next.style.visibility = state.size && to < shown.length ? "visible" : "hidden";
      heads.forEach(function (th, i) {
        if (!th) return;
        th.setAttribute("aria-sort", i === state.col ? (state.dir > 0 ? "ascending" : "descending") : "none");
        th.setAttribute("data-arrow", i === state.col ? (state.dir > 0 ? " ▲" : " ▼") : "");
      });
    }

    search.addEventListener("keydown", function (e) { if (e.key === "Enter") e.preventDefault(); });  // never submits a form around the table
    search.addEventListener("input", function () { state.query = search.value.trim().toLowerCase(); state.page = 0; draw(); });
    sizes.addEventListener("change", function () { state.size = parseInt(sizes.value, 10); state.page = 0; draw(); });
    prev.addEventListener("click", function () { state.page -= 1; draw(); });
    next.addEventListener("click", function () { state.page += 1; draw(); });
    draw();
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("table.data-table"), enhance);
  });
})();

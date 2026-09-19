(function(){
  "use strict";

  // Categorical contract palette, tuned to sit alongside the teal/amber/red M3 system.
  var CONTRACT_COLORS = ["#3E9882","#3E8898","#3E6198","#413E98","#683E98","#8E3E98",
    "#983E7B","#983E55","#984E3E","#98753E","#95983E","#6E983E","#48983E","#3E985B"];

  var SCENARIO_META = {
    A: {label:"Scenario A", sub:"Strict supply · ECLO forbidden"},
    B: {label:"Scenario B", sub:"Strict schedule · supply soft"},
    C: {label:"Scenario C", sub:"Balanced trade-off"}
  };

  var DATA = {params:{horizon_start:"2027-01-04",horizon_weeks:30}, lines:[], stations:[], sectors:[],
    location_supply:{}, contracts:{}, activities:{}, scenarios:{}};

  var contractIds = [];
  var activityIds = [];
  var contractColor = {};
  var horizonStart = new Date(DATA.params.horizon_start + "T00:00:00");
  var horizonWeeks = DATA.params.horizon_weeks;
  var lastKnownHash = null;

  function hexToRgba(hex, alpha){
    var r = parseInt(hex.slice(1,3),16), g = parseInt(hex.slice(3,5),16), b = parseInt(hex.slice(5,7),16);
    return "rgba("+r+","+g+","+b+","+alpha+")";
  }

  function weekToDate(week, dayOffset){
    var d = new Date(horizonStart.getTime());
    d.setDate(d.getDate() + (week - 1) * 7 + (dayOffset || 0));
    return d;
  }
  var MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  function fmtDate(dateStr){
    var d = new Date(dateStr + "T00:00:00");
    return MONTHS[d.getMonth()] + " " + d.getDate() + ", " + d.getFullYear();
  }
  function fmtDateObj(d){
    return MONTHS[d.getMonth()] + " " + d.getDate() + ", " + d.getFullYear();
  }
  function timeAgo(iso){
    var s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if(s < 5) return "just now";
    if(s < 60) return Math.floor(s) + "s ago";
    if(s < 3600) return Math.floor(s/60) + "m ago";
    return Math.floor(s/3600) + "h ago";
  }

  // ---------------- toast ----------------
  var toastTimer = null;
  function toast(msg, isError){
    var el = document.getElementById("toast");
    el.textContent = msg;
    el.className = isError ? "show error" : "show";
    if(toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function(){ el.classList.remove("show"); }, 3400);
  }

  // ---------------- API ----------------
  var SERVER_HINT = "Can't reach the server at " + window.location.origin +
    " — start it with `python app/server.py` from the repo root, then reload this page.";

  function describeNetworkError(err){
    // A rejected fetch() (before any response) means the server isn't running/reachable at
    // all — distinct from an HTTP-level error the server itself returned as JSON.
    if(err instanceof TypeError) return new Error(SERVER_HINT);
    return err;
  }
  function apiGet(url){
    return fetch(url, {cache:"no-store"}).catch(function(err){ throw describeNetworkError(err); })
      .then(function(res){
        return res.json().then(function(body){
          if(!res.ok) throw new Error(body.error || ("HTTP " + res.status));
          return body;
        });
      });
  }
  function apiPost(url){
    return fetch(url, {method:"POST", cache:"no-store"}).catch(function(err){ throw describeNetworkError(err); })
      .then(function(res){
        return res.json().then(function(body){
          if(!res.ok) throw new Error(body.error || ("HTTP " + res.status));
          return body;
        });
      });
  }

  // ---------------- state ----------------
  var state = {
    scenario: "A",
    view: "map",
    search: "",
    lineFilter: null,
    selectedContracts: new Set(),
    collapsedContracts: new Set(),
    week: 1,
    playing: false,
    playTimer: null,
    spotlight: null,
    drawerActivity: null,
    booted: false,
    configured: false
  };

  function scenarioData(){ return DATA.scenarios[state.scenario]; }

  // ---------------- filtering ----------------
  function matchesSearch(activity){
    if(!state.search) return true;
    var q = state.search.toLowerCase();
    return activity.activity_id.toLowerCase().indexOf(q) >= 0 ||
           activity.contract_number.toLowerCase().indexOf(q) >= 0 ||
           (DATA.contracts[activity.contract_number].description || "").toLowerCase().indexOf(q) >= 0;
  }
  function activityPasses(activity){
    if(state.lineFilter && activity.line !== state.lineFilter) return false;
    if(!state.selectedContracts.has(activity.contract_number)) return false;
    if(!matchesSearch(activity)) return false;
    return true;
  }
  function filteredActivityIds(){
    return activityIds.filter(function(aid){ return activityPasses(DATA.activities[aid]); });
  }

  // ---------------- scenario tabs + reschedule + status line ----------------
  function renderScenarioTabs(){
    var el = document.getElementById("scenarioTabs");
    el.innerHTML = "";
    ["A","B","C"].forEach(function(s){
      var btn = document.createElement("button");
      btn.className = "scenario-tab" + (state.scenario === s ? " active" : "");
      btn.setAttribute("role","tab");
      btn.innerHTML = "<strong>"+SCENARIO_META[s].label+"</strong><span>"+SCENARIO_META[s].sub+"</span>";
      btn.addEventListener("click", function(){
        if(state.scenario === s) return;
        state.scenario = s;
        state.week = Math.min(state.week, horizonWeeks);
        state.spotlight = null;
        closeDrawer();
        switchScenario(s);
      });
      el.appendChild(btn);
    });
  }

  function renderStatusLine(){
    var el = document.getElementById("statusLine");
    var sc = scenarioData();
    if(!sc){ el.innerHTML = ""; return; }
    var r = sc.report;
    var parts = [];
    parts.push("<span class='status-dot " + (r.feasible ? "good" : "bad") + "'></span>" +
      "<span>" + (r.feasible ? "Feasible" : "Infeasible") + "</span>");
    parts.push("<span class='sep'>·</span><span>overrun " + r.soft_scores.overrun_days_total + "</span>");
    if(r.hard_violations.length){
      parts.push("<span class='sep'>·</span><span class='pill bad'>" + r.hard_violations.length + " hard violation(s)</span>");
    }
    parts.push("<span class='sep'>·</span><span>solved via " + sc.engine_used + " in " + sc.elapsed_seconds + "s</span>");
    parts.push("<span class='sep'>·</span><span>updated " + timeAgo(sc.solved_at) + "</span>");
    el.innerHTML = parts.join(" ");
  }

  function setRescheduling(on){
    var btn = document.getElementById("rescheduleBtn");
    btn.classList.toggle("busy", on);
    btn.disabled = on;
    document.getElementById("rescheduleLabel").textContent = on ? "Rescheduling…" : "Reschedule";
  }

  document.getElementById("rescheduleBtn").addEventListener("click", function(){
    setRescheduling(true);
    apiPost("/api/reschedule/" + state.scenario).then(function(payload){
      DATA.scenarios[state.scenario] = payload;
      renderStatusLine();
      renderActiveView();
      toast("Scenario " + state.scenario + " rescheduled.");
    }).catch(function(err){
      toast("Reschedule failed: " + err.message, true);
    }).finally(function(){
      setRescheduling(false);
    });
  });

  // ---------------- render: sidebar filters ----------------
  function renderLineFilter(){
    var el = document.getElementById("lineFilter");
    el.innerHTML = "";
    var opts = [{code:null,label:"All lines",cls:""}].concat(
      DATA.lines.map(function(l){ return {code:l.code, label:l.name, cls:"line-"+l.code.toLowerCase()}; })
    );
    opts.forEach(function(o){
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip " + o.cls + (state.lineFilter === o.code ? " active" : "");
      chip.innerHTML = (o.code ? "<span class='dot' style='color:"+(o.code==='ALP'?'var(--line-alp)':'var(--line-bet)')+"'></span>" : "") + o.label;
      chip.addEventListener("click", function(){
        state.lineFilter = o.code;
        renderLineFilter();
        renderActiveView();
        updateResultCount();
      });
      el.appendChild(chip);
    });
  }

  function renderContractList(){
    var el = document.getElementById("contractList");
    el.innerHTML = "";
    contractIds.forEach(function(cid){
      var c = DATA.contracts[cid];
      var row = document.createElement("label");
      row.className = "contract-row" + (state.selectedContracts.has(cid) ? "" : " dimmed");
      var checked = state.selectedContracts.has(cid);
      row.innerHTML =
        "<input type='checkbox' " + (checked ? "checked" : "") + ">" +
        "<span class='contract-swatch' style='background:"+contractColor[cid]+"'></span>" +
        "<span class='cnum mono'>"+cid+"</span>" +
        "<span class='cdesc' title='"+c.description+"'>"+c.description+"</span>";
      var cb = row.querySelector("input");
      cb.addEventListener("change", function(){
        if(cb.checked) state.selectedContracts.add(cid); else state.selectedContracts.delete(cid);
        row.classList.toggle("dimmed", !cb.checked);
        renderActiveView(); updateResultCount();
      });
      el.appendChild(row);
    });
  }

  function updateResultCount(){
    var el = document.getElementById("resultCount");
    if(!el) return;
    var n = filteredActivityIds().length;
    el.textContent = n + " of " + activityIds.length + " activities";
  }

  // ---------------- Gantt ----------------
  function weekPct(week){ return ((week - 1) / horizonWeeks) * 100; }

  function renderGanttAxis(){
    var axis = document.getElementById("ganttAxis");
    if(!axis) return;
    axis.innerHTML = "";
    var spacer = document.createElement("div");
    spacer.className = "axis-spacer";
    axis.appendChild(spacer);
    var track = document.createElement("div");
    track.className = "axis-track";
    var lastMonth = null;
    for(var w = 1; w <= horizonWeeks; w++){
      var d = weekToDate(w, 0);
      var m = d.getMonth() + "-" + d.getFullYear();
      if(m !== lastMonth){
        lastMonth = m;
        var marker = document.createElement("div");
        marker.className = "axis-month";
        marker.style.left = weekPct(w) + "%";
        marker.textContent = MONTHS[d.getMonth()] + " " + d.getFullYear();
        track.appendChild(marker);
      }
    }
    axis.appendChild(track);
  }

  function gridLines(container){
    var lastMonth = null;
    for(var w = 1; w <= horizonWeeks; w++){
      var d = weekToDate(w, 0);
      var m = d.getMonth() + "-" + d.getFullYear();
      if(m !== lastMonth){
        lastMonth = m;
        var line = document.createElement("div");
        line.className = "track-grid-line";
        line.style.left = weekPct(w) + "%";
        container.appendChild(line);
      }
    }
  }

  function ganttMarkup(){
    return "<div class='gantt-scroll'>" +
      "<div class='gantt-axis' id='ganttAxis'></div>" +
      "<div class='gantt-body' id='ganttBody'></div>" +
    "</div>";
  }

  function renderGantt(){
    if(state.view !== "gantt") return;
    if(!document.getElementById("ganttAxis")){
      document.getElementById("panelBody").innerHTML = ganttMarkup();
    }
    renderGanttAxis();
    var body = document.getElementById("ganttBody");
    body.innerHTML = "";
    var sc = scenarioData();
    if(!sc) return;
    var visible = filteredActivityIds();
    var byContract = {};
    visible.forEach(function(aid){
      var c = DATA.activities[aid].contract_number;
      (byContract[c] = byContract[c] || []).push(aid);
    });
    var visibleContracts = contractIds.filter(function(c){ return byContract[c] && byContract[c].length; });

    if(!visibleContracts.length){
      var empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "No activities match the current filters.";
      body.appendChild(empty);
      updateResultCount();
      return;
    }

    visibleContracts.forEach(function(cid){
      var contract = DATA.contracts[cid];
      var result = sc.results[cid];
      var group = document.createElement("div");
      group.className = "contract-group" + (state.collapsedContracts.has(cid) ? " collapsed" : "");

      var header = document.createElement("div");
      header.className = "contract-group-header";
      var overrun = result.overrun_days > 0;
      var sortedActs = byContract[cid].slice().sort(function(a,b){
        return sc.activities[a].start_week - sc.activities[b].start_week;
      });
      header.innerHTML =
        "<div class='cgh-label'>" +
          "<span class='cgh-caret msi'>expand_more</span>" +
          "<span class='cgh-swatch' style='background:"+contractColor[cid]+"'></span>" +
          "<div class='cgh-text'>" +
            "<div class='cgh-num'>"+cid+" <span style='color:var(--text-faint);font-weight:500'>· "+byContract[cid].length+" act.</span></div>" +
            "<div class='cgh-desc'>"+contract.description+"</div>" +
          "</div>" +
        "</div>" +
        "<div class='cgh-right'>" +
          "<div class='cgh-status'>" +
            "<span class='pill "+(overrun?"warn":"good")+"'>"+(overrun ? ("+" + result.overrun_days + "d overrun") : "on schedule")+"</span>" +
          "</div>" +
          "<div class='cgh-track' title='Collapsed — showing all "+sortedActs.length+" activities for "+cid+"'></div>" +
        "</div>";
      var miniTrack = header.querySelector(".cgh-track");
      gridLines(miniTrack);
      var laneH = Math.max(38 / sortedActs.length, 3);
      sortedActs.forEach(function(aid, i){
        var sched = sc.activities[aid];
        var left = weekPct(sched.start_week);
        var width = Math.max(weekPct(sched.end_week + 1) - left, 0.6);
        var mini = document.createElement("div");
        mini.className = "cgh-mini-bar" + (sched.eclo_nights > 0 ? " eclo" : "");
        mini.style.left = left + "%";
        mini.style.width = width + "%";
        mini.style.top = (i * laneH) + "px";
        mini.style.background = contractColor[cid];
        mini.title = aid + " · W" + sched.start_week + "–W" + sched.end_week;
        miniTrack.appendChild(mini);
      });
      header.addEventListener("click", function(){
        if(state.collapsedContracts.has(cid)) state.collapsedContracts.delete(cid);
        else state.collapsedContracts.add(cid);
        group.classList.toggle("collapsed");
      });
      group.appendChild(header);

      var rows = document.createElement("div");
      rows.className = "contract-rows";
      sortedActs.forEach(function(aid){
        rows.appendChild(buildActivityRow(aid));
      });
      group.appendChild(rows);
      body.appendChild(group);
    });
    updateResultCount();
  }

  function buildActivityRow(aid){
    var activity = DATA.activities[aid];
    var sc = scenarioData();
    var sched = sc.activities[aid];
    var row = document.createElement("div");
    row.className = "activity-row";

    var label = document.createElement("div");
    label.className = "arow-label";
    label.innerHTML =
      "<span class='arow-id'>"+aid+"</span>" +
      "<span class='arow-tags'>" +
        "<span class='tag'>"+activity.line+"</span>" +
        "<span class='tag'>P"+activity.activity_priority+"</span>" +
      "</span>";
    row.appendChild(label);

    var track = document.createElement("div");
    track.className = "arow-track";
    gridLines(track);

    var bar = document.createElement("div");
    bar.className = "gbar";
    var left = weekPct(sched.start_week);
    var width = Math.max(weekPct(sched.end_week + 1) - left, 1.1);
    bar.style.left = left + "%";
    bar.style.width = width + "%";
    var cColorBar = contractColor[activity.contract_number];
    bar.style.borderColor = cColorBar;
    bar.style.background = hexToRgba(cColorBar, 0.16);
    bar.style.color = cColorBar;
    sched.accesses.forEach(function(a){
      var tick = document.createElement("div");
      tick.className = "tick" + (a.eclo ? " eclo" : "");
      var tickLeft = ((weekPct(a.week) - left) / width) * 100;
      tick.style.left = Math.max(Math.min(tickLeft, 96), 1) + "%";
      tick.title = "Week " + a.week + (a.eclo ? " · ECLO" : "");
      bar.appendChild(tick);
    });
    bar.title = aid + " · weeks " + sched.start_week + "–" + sched.end_week +
      " (" + fmtDate(sched.start_date) + " – " + fmtDate(sched.end_date) + ")";
    bar.addEventListener("click", function(){ openDrawer(aid); });
    track.appendChild(bar);
    row.appendChild(track);
    return row;
  }

  // ---------------- Line map ----------------
  var COL_SPACING = 92, COL_MARGIN = 112;
  var LINE_ROW = {ALP: 172, BET: 362};
  var CHIP_W = 96, CHIP_H = 34;
  var ACTIVITY_ICONS = {Renewal: "build", Construction: "construction"};
  var mapGeom = null;

  function buildMapGeometry(){
    var geom = {lines:{}, maxCols:0};
    DATA.lines.forEach(function(line){
      var stations = DATA.stations.filter(function(s){ return s.line === line.code; })
        .sort(function(a,b){ return a.seq - b.seq; });
      var colX = {};
      stations.forEach(function(s,i){ colX[s.id] = COL_MARGIN + i * COL_SPACING; });
      var sectors = DATA.sectors.filter(function(s){ return s.line === line.code; })
        .sort(function(a,b){ return a.seq - b.seq; });
      geom.lines[line.code] = {stations: stations, colX: colX, sectors: sectors, name: line.name};
      geom.maxCols = Math.max(geom.maxCols, stations.length);
    });
    geom.width = COL_MARGIN * 2 + (geom.maxCols - 1) * COL_SPACING;
    geom.height = 480;
    return geom;
  }

  function contractOfActivity(aid){ return DATA.activities[aid].contract_number; }

  function svgEl(tag, attrs){
    var el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for(var k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }

  function mapMarkup(){
    return "" +
    "<div class='map-head'>" +
      "<div class='map-head-title'><span class='live-dot'></span> Line network — Scenario " + state.scenario + "</div>" +
      "<div class='map-head-legend'>" +
        "<span class='item'><span class='msi'>construction</span> Scheduled work</span>" +
        "<span class='item'><span class='msi'>diversity_3</span> Co-sharing</span>" +
        "<span class='item'><span class='msi'>report</span> Over capacity</span>" +
      "</div>" +
    "</div>" +
    "<div class='map-controls'>" +
      "<button class='icon-btn' id='weekPrev' type='button' aria-label='Previous week'><span class='msi'>chevron_left</span></button>" +
      "<input type='range' id='weekSlider' min='1' max='" + horizonWeeks + "' value='" + state.week + "'>" +
      "<button class='icon-btn' id='weekNext' type='button' aria-label='Next week'><span class='msi'>chevron_right</span></button>" +
      "<div class='week-readout'><strong id='weekLabel'>Week 1</strong><span id='weekDate'></span></div>" +
      "<label class='play-toggle'><input type='checkbox' id='weekPlay'> Auto-play</label>" +
      "<span id='spotlightPill'></span>" +
    "</div>" +
    "<div class='map-wrap'><svg id='lineMapSvg' xmlns='http://www.w3.org/2000/svg'></svg></div>" +
    "<div class='map-legend' id='mapLegend'></div>";
  }

  function wireMapControls(){
    document.getElementById("weekSlider").addEventListener("input", function(e){
      stopPlay();
      setWeek(parseInt(e.target.value, 10));
    });
    document.getElementById("weekPrev").addEventListener("click", function(){ stopPlay(); setWeek(state.week - 1); });
    document.getElementById("weekNext").addEventListener("click", function(){ stopPlay(); setWeek(state.week + 1); });
    document.getElementById("weekPlay").addEventListener("change", function(e){
      if(e.target.checked){
        state.playing = true;
        state.playTimer = setInterval(function(){
          var next = state.week + 1;
          if(next > horizonWeeks) next = 1;
          setWeek(next);
        }, 900);
      } else {
        stopPlay();
      }
    });
  }

  // locId "SEC:BET:S15_S16:EB" / "PLAT:ALP:S03:EB" -> the line code, always segment [1]
  function lineOfLocation(locId){ return locId.split(":")[1]; }

  function renderMap(){
    if(state.view !== "map") return;
    if(!document.getElementById("lineMapSvg")){
      document.getElementById("panelBody").innerHTML = mapMarkup();
      wireMapControls();
    }
    if(!mapGeom) mapGeom = buildMapGeometry();
    var svg = document.getElementById("lineMapSvg");
    svg.setAttribute("viewBox", "0 0 " + mapGeom.width + " " + mapGeom.height);
    svg.innerHTML = "";

    var defs = svgEl("defs", {});
    var pattern = svgEl("pattern", {id:"hatchMulti", patternUnits:"userSpaceOnUse", width:6, height:6, patternTransform:"rotate(45)"});
    pattern.appendChild(svgEl("rect", {width:6, height:6, fill:"var(--accent-soft)"}));
    pattern.appendChild(svgEl("line", {x1:0,y1:0,x2:0,y2:6, stroke:"var(--accent)", "stroke-width":2.2}));
    defs.appendChild(pattern);
    var glow = svgEl("radialGradient", {id:"junctionGlow", cx:"50%", cy:"50%", r:"65%"});
    glow.appendChild(svgEl("stop", {offset:"0%", "stop-color":"var(--warn)", "stop-opacity":0.16}));
    glow.appendChild(svgEl("stop", {offset:"100%", "stop-color":"var(--warn)", "stop-opacity":0}));
    defs.appendChild(glow);
    svg.appendChild(defs);

    var sc = scenarioData();
    if(!sc) return;
    var occupants = (sc.weekly_location_map[String(state.week)]) || {};
    var filteredSet = new Set(filteredActivityIds());
    var hotspotSet = {};
    sc.report.detail.capacity_hotspots.forEach(function(h){
      if(h.excess > 0) hotspotSet[h.location_id + "|" + h.week] = true;
    });

    // ---- shared H01/H02 interchange zone (structural — always shown, not week-dependent) ----
    var interStations = {};
    DATA.lines.forEach(function(line){
      mapGeom.lines[line.code].stations.forEach(function(s){
        if(s.interchange){
          interStations[s.id] = interStations[s.id] || [];
          interStations[s.id].push(line.code);
        }
      });
    });
    var interIds = Object.keys(interStations).filter(function(sid){ return interStations[sid].length >= 2; });
    if(interIds.length){
      var xs = interIds.map(function(sid){ return mapGeom.lines[interStations[sid][0]].colX[sid]; });
      var zoneX1 = Math.min.apply(null, xs) - 34, zoneX2 = Math.max.apply(null, xs) + 34;
      svg.appendChild(svgEl("rect", {
        x:zoneX1, y:LINE_ROW.ALP - 20, width:zoneX2 - zoneX1, height:(LINE_ROW.BET - LINE_ROW.ALP) + 40,
        rx:16, fill:"url(#junctionGlow)", class:"interchange-link"
      }));
      var zoneLabel = svgEl("text", {x:(zoneX1+zoneX2)/2, y:(LINE_ROW.ALP+LINE_ROW.BET)/2 + 4, "text-anchor":"middle", class:"map-station-label", "font-size":9.5, "font-weight":700, fill:"var(--warn)"});
      zoneLabel.textContent = "SHARED JUNCTION H01–H02";
      svg.appendChild(zoneLabel);
      interIds.forEach(function(sid){
        var codes = interStations[sid];
        var x = mapGeom.lines[codes[0]].colX[sid];
        svg.appendChild(svgEl("line", {x1:x,y1:LINE_ROW[codes[0]]+18,x2:x,y2:LINE_ROW[codes[1]]-18, stroke:"var(--warn)", "stroke-width":1.4, "stroke-dasharray":"3,4", opacity:0.6, class:"interchange-link"}));
      });
    }

    var chipAnchors = []; // {x, line, activityId} — gathered while drawing, chips placed after
    var seenChipActivity = {};

    DATA.lines.forEach(function(line){
      var g = mapGeom.lines[line.code];
      var y = LINE_ROW[line.code];
      var lineColor = line.code === "ALP" ? "var(--line-alp)" : "var(--line-bet)";
      var firstX = g.colX[g.stations[0].id], lastX = g.colX[g.stations[g.stations.length-1].id];

      svg.appendChild(svgEl("rect", {x:10, y:y-11, width:88, height:22, rx:11, fill:lineColor, opacity:0.14, class:"map-line-label"}));
      var lineLabel = svgEl("text", {x:54, y:y+4, "text-anchor":"middle", class:"map-line-label", "font-size":10.5, fill:lineColor});
      lineLabel.textContent = line.name;
      svg.appendChild(lineLabel);

      // base track
      svg.appendChild(svgEl("line", {x1:firstX, y1:y, x2:lastX, y2:y, stroke:"var(--border)", "stroke-width":5, "stroke-linecap":"round"}));

      g.sectors.forEach(function(sector){
        var x1 = g.colX[sector.from], x2 = g.colX[sector.to];
        if(x1 == null || x2 == null) return;
        var occEB = occupants[sector.id + ":EB"] || [], occWB = occupants[sector.id + ":WB"] || [];
        var occ = uniq(occEB.concat(occWB));
        var isHot = hotspotSet[sector.id + ":EB|" + state.week] || hotspotSet[sector.id + ":WB|" + state.week];
        drawTrackSegment(svg, x1, x2, y, occ, filteredSet, isHot, sector.id);
        registerChipAnchors(chipAnchors, occ, filteredSet, (x1+x2)/2, line.code);
      });

      g.stations.forEach(function(st){
        var x = g.colX[st.id];
        var occEB = occupants["PLAT:" + line.code + ":" + st.id + ":EB"] || [];
        var occWB = occupants["PLAT:" + line.code + ":" + st.id + ":WB"] || [];
        var occ = uniq(occEB.concat(occWB));
        var isHot = hotspotSet["PLAT:"+line.code+":"+st.id+":EB|"+state.week] || hotspotSet["PLAT:"+line.code+":"+st.id+":WB|"+state.week];
        drawStationNode(svg, x, y, st, occ, filteredSet, isHot);
        registerChipAnchors(chipAnchors, occ, filteredSet, x, line.code);

        var labelY = line.code === "ALP" ? y - 28 : y + 38;
        var label = svgEl("text", {x:x, y:labelY, "text-anchor":"middle", class:"map-station-label", "font-size":9.5, fill: st.interchange ? "var(--text)" : "var(--text-faint)", "font-weight": st.interchange ? 700 : 400});
        label.textContent = st.id;
        svg.appendChild(label);
      });
    });

    renderChips(svg, chipAnchors, sc);
    renderMapLegend(occupants, filteredSet);
    updateWeekReadout();
    renderSpotlightPill();
  }

  function uniq(arr){
    var seen = {}, out = [];
    arr.forEach(function(a){ if(!seen[a]){ seen[a]=true; out.push(a); } });
    return out;
  }

  function cellVisual(occ, filteredSet, isHot){
    var included = occ.filter(function(a){ return filteredSet.has(a); });
    var excluded = occ.filter(function(a){ return !filteredSet.has(a); });
    var fill = null, stroke = "var(--border)", strokeWidth = 1, opacity = 1, dash = null;

    if(state.spotlight){
      var isSpot = occ.indexOf(state.spotlight) >= 0;
      if(isSpot){ fill = contractColor[contractOfActivity(state.spotlight)]; stroke = "var(--accent)"; strokeWidth = 2.4; }
      else { opacity = occ.length ? 0.3 : 0.55; }
    } else if(included.length === 1){
      fill = contractColor[contractOfActivity(included[0])];
    } else if(included.length > 1){
      fill = "url(#hatchMulti)"; stroke = "var(--accent)"; strokeWidth = 1.8;
    } else if(excluded.length > 0){
      opacity = 0.55; dash = "2,2";
    }
    if(isHot && !state.spotlight){ stroke = "var(--bad)"; strokeWidth = 2.4; }
    return {fill:fill, stroke:stroke, strokeWidth:strokeWidth, opacity:opacity, dash:dash, included:included, excluded:excluded};
  }

  function drawTrackSegment(svg, x1, x2, y, occ, filteredSet, isHot, sectorId){
    var v = cellVisual(occ, filteredSet, isHot);
    if(v.fill){
      var seg = svgEl("line", {
        x1:x1+16, y1:y, x2:x2-16, y2:y, stroke:v.fill, "stroke-width":7, "stroke-linecap":"round",
        opacity:v.opacity, class:"loc-cell"
      });
      if(v.stroke !== "var(--border)"){
        svg.appendChild(svgEl("line", {x1:x1+16,y1:y,x2:x2-16,y2:y, stroke:v.stroke, "stroke-width":10, "stroke-linecap":"round", opacity:0.35, "pointer-events":"none"}));
      }
      var title = svgEl("title", {});
      title.textContent = sectorId + "\nWeek " + state.week + " · " + occ.length + " occupant(s)" + (occ.length ? "\n" + occ.join(", ") : "");
      seg.appendChild(title);
      seg.addEventListener("click", function(ev){ ev.stopPropagation(); handleCellClick(ev, sectorId, occ, v.included, v.excluded); });
      svg.appendChild(seg);
    }
  }

  function drawStationNode(svg, x, y, st, occ, filteredSet, isHot){
    var v = cellVisual(occ, filteredSet, isHot);
    var r = st.interchange ? 11 : 8;
    var circle = svgEl("circle", {
      cx:x, cy:y, r:r, fill: v.fill || "var(--surface)",
      stroke: v.fill ? v.stroke : (st.interchange ? "var(--warn)" : "var(--text-faint)"),
      "stroke-width": v.fill ? v.strokeWidth : (st.interchange ? 2.4 : 1.6),
      opacity:v.opacity, class:"loc-cell"
    });
    var title = svgEl("title", {});
    title.textContent = "PLAT:" + st.line + ":" + st.id + "\nWeek " + state.week + " · " + occ.length + " occupant(s)" + (occ.length ? "\n" + occ.join(", ") : "\nfree");
    circle.appendChild(title);
    circle.addEventListener("click", function(ev){ ev.stopPropagation(); handleCellClick(ev, "PLAT:"+st.line+":"+st.id, occ, v.included, v.excluded); });
    svg.appendChild(circle);
  }

  function registerChipAnchors(chipAnchors, occ, filteredSet, x, lineCode){
    occ.forEach(function(aid){
      if(state.spotlight ? aid !== state.spotlight : !filteredSet.has(aid)) return;
      var key = lineCode + "|" + aid;
      var existing = chipAnchors.find(function(c){ return c.key === key; });
      if(existing){ existing.xs.push(x); }
      else { chipAnchors.push({key:key, line:lineCode, activityId:aid, xs:[x]}); }
    });
  }

  function chipStatusText(aid, sc){
    var sched = sc.activities[aid];
    var activity = DATA.activities[aid];
    var thisWeek = sched.accesses.filter(function(a){ return a.week === state.week; });
    var eclo = thisWeek.some(function(a){ return a.eclo; });
    var seqs = thisWeek.map(function(a){ return a.seq; }).join(",");
    var text = "Night " + seqs + " of " + activity.total_accesses;
    return {text: text, eclo: eclo};
  }

  function renderChips(svg, chipAnchors, sc){
    ["ALP","BET"].forEach(function(lineCode){
      var chips = chipAnchors.filter(function(c){ return c.line === lineCode; })
        .map(function(c){ return {activityId:c.activityId, x: c.xs.reduce(function(a,b){return a+b;},0)/c.xs.length}; })
        .sort(function(a,b){ return a.x - b.x; });
      // simple left-to-right collision push so chips never overlap
      for(var i=1;i<chips.length;i++){
        var minX = chips[i-1].x + CHIP_W + 6;
        if(chips[i].x < minX) chips[i].x = minX;
      }
      var above = lineCode === "ALP";
      var y = above ? LINE_ROW[lineCode] - 44 : LINE_ROW[lineCode] + 44;
      chips.forEach(function(c){
        drawChip(svg, c.x, y, above, c.activityId, sc);
      });
    });
  }

  function drawChip(svg, cx, edgeY, above, aid, sc){
    var activity = DATA.activities[aid];
    var color = contractColor[activity.contract_number];
    var status = chipStatusText(aid, sc);
    var top = above ? edgeY - CHIP_H : edgeY;
    var g = svgEl("g", {class:"map-chip", "data-activity":aid, style:"cursor:pointer"});
    g.appendChild(svgEl("rect", {
      x:cx-CHIP_W/2, y:top, width:CHIP_W, height:CHIP_H, rx:9,
      fill:"var(--surface)", stroke:color, "stroke-width": state.spotlight===aid ? 2.4 : 1.4
    }));
    var icon = svgEl("text", {x:cx-CHIP_W/2+15, y:top+16, "text-anchor":"middle", class:"msi", "font-size":13, fill:color});
    icon.textContent = status.eclo ? "nights_stay" : (ACTIVITY_ICONS[activity.activity_type] || "build");
    g.appendChild(icon);
    var idText = svgEl("text", {x:cx-CHIP_W/2+26, y:top+15, class:"map-chip-id", "font-size":10.5, "font-weight":700, fill:"var(--text)"});
    idText.textContent = aid;
    g.appendChild(idText);
    var statusEl = svgEl("text", {x:cx-CHIP_W/2+26, y:top+27, class:"map-chip-status", "font-size":8.5, fill:"var(--text-faint)"});
    statusEl.textContent = status.text;
    g.appendChild(statusEl);
    var title = svgEl("title", {});
    title.textContent = aid + " (" + activity.contract_number + ") — " + status.text + (status.eclo ? " · ECLO" : "");
    g.appendChild(title);
    g.addEventListener("click", function(ev){ ev.stopPropagation(); closePopover(); openDrawer(aid); });
    svg.appendChild(g);
  }

  function renderMapLegend(occupants, filteredSet){
    var el = document.getElementById("mapLegend");
    el.innerHTML = "";
    // point 9: only contracts actually visible on the map this week get a legend swatch
    var visibleContracts = new Set();
    Object.keys(occupants).forEach(function(locId){
      occupants[locId].forEach(function(aid){
        if(filteredSet.has(aid)) visibleContracts.add(contractOfActivity(aid));
      });
    });
    var shown = contractIds.filter(function(c){ return visibleContracts.has(c); });
    if(!shown.length){
      var empty = document.createElement("span");
      empty.className = "item";
      empty.style.color = "var(--text-faint)";
      empty.textContent = "No scheduled work visible this week for the current filters.";
      el.appendChild(empty);
      return;
    }
    shown.forEach(function(cid){
      var item = document.createElement("span");
      item.className = "item";
      item.innerHTML = "<span class='swatch' style='background:"+contractColor[cid]+"'></span>"+cid;
      el.appendChild(item);
    });
  }

  function updateWeekReadout(){
    var slider = document.getElementById("weekSlider");
    if(!slider) return;
    slider.value = state.week;
    document.getElementById("weekLabel").textContent = "Week " + state.week + " / " + horizonWeeks;
    var start = weekToDate(state.week, 0), end = weekToDate(state.week, 6);
    document.getElementById("weekDate").textContent = fmtDateObj(start) + " – " + fmtDateObj(end);
  }

  function renderSpotlightPill(){
    var el = document.getElementById("spotlightPill");
    if(!el) return;
    if(!state.spotlight){ el.innerHTML = ""; return; }
    el.innerHTML = "<span class='spotlight-pill'>Spotlight: " + state.spotlight + " <button type='button' id='clearSpotlight'><span class='msi' style='font-size:13px'>close</span></button></span>";
    document.getElementById("clearSpotlight").addEventListener("click", function(){
      state.spotlight = null;
      renderMap();
    });
  }

  var popoverEl = null;
  function closePopover(){
    if(popoverEl){ popoverEl.remove(); popoverEl = null; }
  }
  function handleCellClick(ev, locId, occ, included, excluded){
    closePopover();
    var list = included.length ? included : occ;
    if(list.length === 1 && !excluded.length){
      openDrawer(list[0]);
      return;
    }
    if(!list.length){ return; }
    var pop = document.createElement("div");
    pop.className = "map-popover";
    var rect = ev.target.getBoundingClientRect();
    pop.style.left = Math.min(rect.left, window.innerWidth - 260) + "px";
    pop.style.top = Math.min(rect.bottom + 6, window.innerHeight - 160) + "px";
    var title = document.createElement("div");
    title.className = "mp-title";
    title.textContent = locId.replace(/:/g," · ");
    pop.appendChild(title);
    occ.forEach(function(aid){
      var item = document.createElement("div");
      item.className = "mp-item";
      var cid = contractOfActivity(aid);
      var outside = excluded.indexOf(aid) >= 0;
      item.style.opacity = outside ? 0.55 : 1;
      item.innerHTML = "<span class='dot' style='background:"+contractColor[cid]+"'></span><span class='aid'>"+aid+"</span><span class='cnum'>"+cid+(outside?" · filtered out":"")+"</span>";
      item.addEventListener("click", function(){ closePopover(); openDrawer(aid); });
      pop.appendChild(item);
    });
    document.body.appendChild(pop);
    popoverEl = pop;
  }
  document.addEventListener("click", function(){ closePopover(); });

  function stopPlay(){
    state.playing = false;
    var el = document.getElementById("weekPlay");
    if(el) el.checked = false;
    if(state.playTimer){ clearInterval(state.playTimer); state.playTimer = null; }
  }
  function setWeek(w){
    state.week = Math.max(1, Math.min(horizonWeeks, w));
    renderMap();
  }

  // ---------------- drawer ----------------
  function openDrawer(aid){
    state.drawerActivity = aid;
    renderDrawer(aid);
    document.getElementById("drawer").classList.add("open");
    document.getElementById("drawer").setAttribute("aria-hidden","false");
    document.getElementById("drawerBackdrop").classList.add("open");
  }
  function closeDrawer(){
    document.getElementById("drawer").classList.remove("open");
    document.getElementById("drawer").setAttribute("aria-hidden","true");
    document.getElementById("drawerBackdrop").classList.remove("open");
    state.drawerActivity = null;
  }
  document.getElementById("drawerClose").addEventListener("click", closeDrawer);
  document.getElementById("drawerBackdrop").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", function(e){ if(e.key === "Escape"){ closeDrawer(); closeChat(); } });

  // ---------------- chat assistant (grounded in the live solve() via /api/chat) ----------------
  var chatHistory = [];        // [{role:"user"|"model", content:str}] — sent back each turn for context
  var chatChecked = false;     // have we asked /api/status about chat_available yet this session
  var chatReady = false;

  function chatAppend(role, text){
    var body = document.getElementById("chatBody");
    var el = document.createElement("div");
    el.className = "chat-msg " + role;
    el.textContent = text;
    body.appendChild(el);
    body.scrollTop = body.scrollHeight;
    return el;
  }

  function chatSetReady(ready){
    chatReady = ready;
    document.getElementById("chatInput").disabled = !ready;
    document.getElementById("chatSend").disabled = !ready;
    var badge = document.getElementById("chatBadge");
    badge.textContent = ready ? "Live" : "Setup needed";
    badge.classList.toggle("live", ready);
  }

  function chatShowSetupNotice(){
    var body = document.getElementById("chatBody");
    body.innerHTML = "";
    var el = document.createElement("div");
    el.className = "chat-setup";
    el.innerHTML =
      "This assistant answers questions grounded in a live solve() of the current data " +
      "(the same source shown in the sidebar) — but it needs a free Gemini API key on the server first:" +
      "<br><br>1. Get one at <code>aistudio.google.com/apikey</code> (no billing needed)" +
      "<br>2. Copy <code>.env.example</code> to <code>.env</code> at the repo root and fill in <code>GEMINI_API_KEY</code>" +
      "<br>3. Restart <code>app/server.py</code>" +
      "<br><br>Once set, reopen this panel.";
    body.appendChild(el);
    document.getElementById("chatInput").placeholder = "Chat needs GEMINI_API_KEY set on the server…";
  }

  function chatShowWelcome(){
    document.getElementById("chatBody").innerHTML = "";
    document.getElementById("chatInput").placeholder = "Ask about the schedule…";
    chatAppend("bot",
      "Hi — ask me anything about the current schedule (contracts, delays, capacity pressure, " +
      "why an activity landed where it did) across Scenarios A, B and C. I only answer from the " +
      "real, currently-solved data — I'll say so if something isn't in it.");
  }

  function openChat(){
    document.getElementById("chatPanel").classList.add("open");
    document.getElementById("chatPanel").setAttribute("aria-hidden", "false");
    document.getElementById("chatFab").classList.add("open");
    if(chatChecked) return;
    chatChecked = true;
    apiGet("/api/status").then(function(st){
      chatSetReady(!!st.chat_available);
      if(st.chat_available) chatShowWelcome(); else chatShowSetupNotice();
    }).catch(function(){
      chatSetReady(false);
      chatShowSetupNotice();
    });
  }
  function closeChat(){
    document.getElementById("chatPanel").classList.remove("open");
    document.getElementById("chatPanel").setAttribute("aria-hidden", "true");
    document.getElementById("chatFab").classList.remove("open");
  }
  document.getElementById("chatFab").addEventListener("click", openChat);
  document.getElementById("chatClose").addEventListener("click", closeChat);

  function chatSend(){
    if(!chatReady) return;
    var input = document.getElementById("chatInput");
    var message = input.value.trim();
    if(!message) return;
    input.value = "";
    chatAppend("user", message);
    var typing = document.createElement("div");
    typing.className = "chat-msg bot typing";
    typing.innerHTML = "<span></span><span></span><span></span>";
    var body = document.getElementById("chatBody");
    body.appendChild(typing);
    body.scrollTop = body.scrollHeight;
    document.getElementById("chatInput").disabled = true;
    document.getElementById("chatSend").disabled = true;

    var historyForRequest = chatHistory.slice();
    apiPost2("/api/chat", {message: message, history: historyForRequest})
      .then(function(res){
        typing.remove();
        chatAppend("bot", res.reply);
        chatHistory.push({role: "user", content: message});
        chatHistory.push({role: "model", content: res.reply});
      })
      .catch(function(err){
        typing.remove();
        chatAppend("error", err.message);
      })
      .finally(function(){
        document.getElementById("chatInput").disabled = false;
        document.getElementById("chatSend").disabled = false;
        document.getElementById("chatInput").focus();
      });
  }
  document.getElementById("chatSend").addEventListener("click", chatSend);
  document.getElementById("chatInput").addEventListener("keydown", function(e){
    if(e.key === "Enter") chatSend();
  });

  // ---------------- upload dataset (points 3 & 4: bring your own instance, same features) ----------------
  var REQUIRED_INSTANCE_FILES = ["01_LINES.csv","02_STATIONS.csv","03_SECTORS.csv","04_LOCATION_SUPPLY.csv",
    "05_BUFFER_LOCATION.csv","06_PARAMETERS.csv","07_PROJECT_DETAILS.csv","08_ACTIVITY_DETAILS.csv"];
  var pendingUploadFiles = {}; // canonicalName -> File

  function canonicalInstanceName(filename){
    var stem = filename.split("/").pop().toLowerCase();
    for(var i = 0; i < REQUIRED_INSTANCE_FILES.length; i++){
      var canonical = REQUIRED_INSTANCE_FILES[i];
      var bare = canonical.toLowerCase().split("_").slice(1).join("_"); // "01_LINES.csv" -> "lines.csv"
      if(stem === canonical.toLowerCase() || stem === bare || stem.endsWith("_" + bare)) return canonical;
    }
    return null;
  }

  function openUploadModal(){
    document.getElementById("uploadBackdrop").classList.add("open");
    document.getElementById("uploadModal").classList.add("open");
    document.getElementById("uploadModal").setAttribute("aria-hidden", "false");
    document.getElementById("revertSourceBtn").hidden = DATA.source === "upload" ? false : true;
    document.getElementById("uploadSourceNote").textContent = "Current source: " + (DATA.source_label || DATA.source || "");
    renderUploadChecklist();
  }
  function closeUploadModal(){
    document.getElementById("uploadBackdrop").classList.remove("open");
    document.getElementById("uploadModal").classList.remove("open");
    document.getElementById("uploadModal").setAttribute("aria-hidden", "true");
  }
  document.getElementById("uploadBtn").addEventListener("click", openUploadModal);
  document.getElementById("uploadClose").addEventListener("click", closeUploadModal);
  document.getElementById("uploadBackdrop").addEventListener("click", closeUploadModal);

  function renderUploadChecklist(){
    var el = document.getElementById("uploadChecklist");
    el.innerHTML = "";
    REQUIRED_INSTANCE_FILES.forEach(function(name){
      var matched = !!pendingUploadFiles[name];
      var row = document.createElement("div");
      row.className = "upload-check-row" + (matched ? " matched" : "");
      row.innerHTML = "<span class='msi'>" + (matched ? "check_circle" : "radio_button_unchecked") + "</span><span>" +
        name + (matched ? " — " + escapeHtml(pendingUploadFiles[name].name) : "") + "</span>";
      el.appendChild(row);
    });
    var matchedCount = Object.keys(pendingUploadFiles).length;
    document.getElementById("uploadSubmitBtn").disabled = matchedCount === 0;
  }

  function handlePickedFiles(fileList){
    Array.prototype.forEach.call(fileList, function(f){
      var canonical = canonicalInstanceName(f.name);
      if(canonical) pendingUploadFiles[canonical] = f;
    });
    document.getElementById("uploadError").hidden = true;
    renderUploadChecklist();
  }
  document.getElementById("uploadInput").addEventListener("change", function(e){ handlePickedFiles(e.target.files); });
  var dropZone = document.getElementById("uploadDrop");
  ["dragover","dragenter"].forEach(function(evt){
    dropZone.addEventListener(evt, function(e){ e.preventDefault(); dropZone.classList.add("dragover"); });
  });
  ["dragleave","drop"].forEach(function(evt){
    dropZone.addEventListener(evt, function(e){ e.preventDefault(); dropZone.classList.remove("dragover"); });
  });
  dropZone.addEventListener("drop", function(e){
    if(e.dataTransfer && e.dataTransfer.files) handlePickedFiles(e.dataTransfer.files);
  });

  function readFileAsText(file){
    return new Promise(function(resolve, reject){
      var reader = new FileReader();
      reader.onload = function(){ resolve(reader.result); };
      reader.onerror = function(){ reject(new Error("Could not read " + file.name)); };
      reader.readAsText(file);
    });
  }

  document.getElementById("uploadSubmitBtn").addEventListener("click", function(){
    var names = Object.keys(pendingUploadFiles);
    if(!names.length) return;
    var btn = document.getElementById("uploadSubmitBtn");
    btn.disabled = true;
    btn.innerHTML = "<span class='msi'>hourglass_top</span> Uploading…";
    document.getElementById("uploadError").hidden = true;
    transitionInFlight = true;

    Promise.all(names.map(function(n){ return readFileAsText(pendingUploadFiles[n]).then(function(text){ return [n, text]; }); }))
      .then(function(pairs){
        var files = {};
        pairs.forEach(function(p){ files[p[0]] = p[1]; });
        return fetch("/api/upload", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({files: files})
        }).then(function(res){
          return res.json().then(function(body){
            if(!res.ok) throw new Error(body.error || ("HTTP " + res.status));
            return body;
          });
        });
      })
      .then(function(result){
        pendingUploadFiles = {};
        closeUploadModal();
        toast("Dataset uploaded — rescheduling from it now.");
        return apiGet("/api/meta");
      })
      .then(function(meta){
        if(!meta) return;
        return enterConfigured(meta);
      })
      .catch(function(err){
        var errEl = document.getElementById("uploadError");
        errEl.hidden = false;
        errEl.textContent = err.message;
      })
      .finally(function(){
        btn.disabled = Object.keys(pendingUploadFiles).length === 0;
        btn.innerHTML = "<span class='msi'>check_circle</span> Use this dataset";
        transitionInFlight = false;
      });
  });

  document.getElementById("revertSourceBtn").addEventListener("click", function(){
    var btn = document.getElementById("revertSourceBtn");
    btn.disabled = true;
    transitionInFlight = true;
    apiPost2("/api/source", {source: "github"})
      .then(function(){
        toast("Reverted to the live GitHub source.");
        closeUploadModal();
        return apiGet("/api/meta");
      })
      .then(function(meta){ return enterConfigured(meta); })
      .catch(function(err){ toast("Could not revert source: " + err.message, true); })
      .finally(function(){ btn.disabled = false; transitionInFlight = false; });
  });
  function apiPost2(url, bodyObj){
    return fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(bodyObj)})
      .catch(function(err){ throw describeNetworkError(err); })
      .then(function(res){
        return res.json().then(function(body){
          if(!res.ok) throw new Error(body.error || ("HTTP " + res.status));
          return body;
        });
      });
  }

  document.getElementById("resetBtn").addEventListener("click", function(){
    if(!window.confirm("Reset clears the currently loaded dataset for everyone using this app. Continue?")) return;
    var btn = document.getElementById("resetBtn");
    btn.disabled = true;
    transitionInFlight = true;
    apiPost("/api/reset").then(function(){
      toast("Dataset cleared. Upload a new one to continue.");
      closeUploadModal();
      closeDrawer();
      enterUnconfigured();
    }).catch(function(err){
      toast("Reset failed: " + err.message, true);
    }).finally(function(){
      btn.disabled = false;
      transitionInFlight = false;
    });
  });

  document.getElementById("exportBtn").addEventListener("click", function(){
    var btn = document.getElementById("exportBtn");
    var origHtml = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = "<span class='msi'>hourglass_top</span> Preparing…";
    fetch("/api/export", {cache:"no-store"}).catch(function(err){ throw describeNetworkError(err); })
      .then(function(res){
        if(!res.ok){
          return res.json().then(function(body){ throw new Error(body.error || ("HTTP " + res.status)); });
        }
        return res.blob();
      })
      .then(function(blob){
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = "maintenance_schedule_export.zip";
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        toast("Export downloaded — all scenarios' CSVs + report.json.");
      })
      .catch(function(err){
        toast("Export failed: " + err.message, true);
      })
      .finally(function(){
        btn.disabled = false;
        btn.innerHTML = origHtml;
      });
  });

  function renderDrawer(aid){
    var activity = DATA.activities[aid];
    var contract = DATA.contracts[activity.contract_number];
    var sc = scenarioData();
    var sched = sc.activities[aid];
    var result = sc.results[activity.contract_number];
    var cColor = contractColor[activity.contract_number];

    var html = "";
    html += "<div class='dw-head' style='display:flex;align-items:flex-start;gap:10px'>";
    html += "<span class='msi' style='width:34px;height:34px;border-radius:10px;flex:none;display:flex;align-items:center;justify-content:center;background:"+cColor+"1f;color:"+cColor+";font-size:19px'>train</span>";
    html += "<div>";
    html += "<div class='dw-id'>" + aid + "</div>";
    html += "<div class='dw-sub'>";
    html += "<span class='pill' style='background:"+cColor+"22;color:"+cColor+"'>"+activity.contract_number+"</span>";
    html += "<span class='pill neutral'>"+activity.activity_type+"</span>";
    html += "<span class='pill neutral'>"+activity.line+" line</span>";
    html += "<span class='pill neutral'>priority "+activity.activity_priority+"</span>";
    html += "</div></div></div>";

    html += "<div class='dw-section'><div class='dw-section-title'>Schedule (Scenario "+state.scenario+")</div>";
    html += "<div class='kv-grid'>";
    html += kv("Scheduled window", fmtDate(sched.start_date) + " – " + fmtDate(sched.end_date));
    html += kv("Weeks", "W" + sched.start_week + " → W" + sched.end_week);
    html += kv("Accesses used", sched.accesses.length + " / " + activity.total_accesses + " planned");
    html += kv("ECLO nights", sched.eclo_nights);
    html += kv("Planned start (brief)", fmtDate(activity.planned_start_date));
    html += kv("Track sections touched", sched.occupied_locations.length);
    html += "</div></div>";

    html += "<div class='dw-section'><div class='dw-section-title'>Access nights</div><div class='access-list'>";
    sched.accesses.forEach(function(a){
      html += "<div class='access-row'><span class='aw mono'>Week "+a.week+"</span><span class='ad'>Access night "+a.night+(a.eclo?" &middot; ECLO extended lockout":"")+"</span></div>";
    });
    html += "</div></div>";

    html += "<div class='dw-section'><div class='dw-section-title'>Location span</div>";
    html += "<div class='loc-chain'>"+activity.start_location_id+"<span class='arrow msi'>arrow_forward</span>"+activity.end_location_id+"</div>";
    if(activity.predecessor_activity_id){
      html += "<div style='margin-top:8px'><span style='font-size:10px;color:var(--text-faint);display:block;margin-bottom:5px'>Requires predecessor</span>" +
        "<button class='pred-link' data-goto='"+activity.predecessor_activity_id+"'><span class='msi' style='font-size:14px'>subdirectory_arrow_right</span> "+activity.predecessor_activity_id+"</button></div>";
    }
    html += "</div>";

    html += "<div class='dw-section'><div class='dw-section-title'>Contract "+activity.contract_number+"</div>";
    html += "<div class='kv-grid'>";
    html += kv("Description", contract.description);
    html += kv("Nature", contract.nature);
    html += kv("Contract priority", contract.priority);
    html += kv("Workfronts", contract.workfronts);
    html += kv("Access type", contract.access_type + " · max " + contract.max_access_per_week + "/wk");
    html += kv("Planned completion", fmtDate(contract.planned_completion_date));
    html += kv("Hard deadline", fmtDate(contract.completion_date));
    html += kv("Simulated completion", fmtDate(result.simulated_completion_date));
    html += "</div>";
    html += "<div style='margin-top:10px'><span class='pill "+(result.overrun_days>0?"warn":"good")+"'>" +
      (result.overrun_days > 0 ? ("+" + result.overrun_days + " days overrun") : "completes on schedule") + "</span></div>";
    html += "</div>";

    html += "<button class='btn primary' id='showOnMapBtn'><span class='msi' style='font-size:16px'>map</span> Show on line map</button>";

    document.getElementById("drawerContent").innerHTML = html;

    var predBtn = document.querySelector(".pred-link");
    if(predBtn){
      predBtn.addEventListener("click", function(){ openDrawer(predBtn.dataset.goto); });
    }
    document.getElementById("showOnMapBtn").addEventListener("click", function(){
      state.spotlight = aid;
      var weeks = sched.weeks;
      if(weeks.indexOf(state.week) < 0) state.week = sched.start_week;
      switchView("map");
      closeDrawer();
    });
  }
  function kv(k, v){
    return "<div class='kv'><span class='k'>"+k+"</span><span class='v mono'>"+v+"</span></div>";
  }

  // ---------------- weekly summary ----------------
  // Rethought around src/explain.py's result.explanation (served by the API alongside
  // report.json) rather than restating CSV numbers: a narrated "what actually happened"
  // section, real capacity hotspots with the activities involved, and which activities were
  // delayed/night-clustered and why — not just totals. The per-contract table and weekly
  // chart remain as reference data underneath, now scoped to the sidebar's filters (point 7).
  function renderSummary(){
    var sc = scenarioData();
    if(!sc){ return; }
    var exp = sc.explanation;
    var r = sc.report;
    var filteredSet = new Set(filteredActivityIds());
    var isFiltered = filteredSet.size < activityIds.length;

    var byContract = {};
    contractIds.forEach(function(cid){ byContract[cid] = []; });
    filteredActivityIds().forEach(function(aid){ byContract[DATA.activities[aid].contract_number].push(aid); });
    var visibleContractIds = contractIds.filter(function(cid){ return byContract[cid].length; });

    var weeklyNights = {};
    for(var w = 1; w <= horizonWeeks; w++) weeklyNights[w] = 0;
    filteredActivityIds().forEach(function(aid){
      var sched = sc.activities[aid];
      if(!sched) return;
      sched.accesses.forEach(function(a){ weeklyNights[a.week] = (weeklyNights[a.week] || 0) + 1; });
    });
    var maxWeekNights = Math.max.apply(null, Object.values(weeklyNights).concat([1]));

    var html = "<div class='summary-wrap'>";

    html += "<div class='summary-head'>" +
      "<h2>Weekly summary — Scenario " + state.scenario + "</h2>" +
      "<p>What actually happened when this schedule was built, narrated from the solver's own report" +
      (isFiltered ? " — scoped to " + filteredSet.size + " of " + activityIds.length + " activities matching the current filters" : "") +
      ".</p></div>";

    // ---- 1. narrative: the solver's own plain-English account of the schedule ----
    if(exp && exp.summary && exp.summary.length){
      html += "<div class='summary-card insight-card'><div class='summary-card-title'><span class='msi'>auto_awesome</span> What happened building this schedule</div>";
      html += "<ul class='insight-list' id='cs-narrative'>";
      exp.summary.forEach(function(line){ html += "<li>" + escapeHtml(line) + "</li>"; });
      html += "</ul></div>";
    }

    // ---- 2. capacity pressure: real hotspots, with the activities actually involved ----
    if(exp){
      var cp = exp.capacity_pressure;
      html += "<div class='summary-card'><div class='summary-card-title'><span class='msi'>speed</span> Capacity pressure</div>";
      html += "<div class='compliance-row'>";
      html += "<span class='pill neutral'>" + cp.total_hotspots + " location-week(s) at or above capacity</span>";
      html += "<span class='pill " + (cp.binding_count ? "bad" : "good") + "'>" + cp.binding_count + " over capacity</span>";
      html += "<span class='pill neutral'>" + cp.at_capacity_count + " exactly full</span>";
      html += "<span class='pill " + (r.hard_violations.length ? "bad" : "good") + "'>" + r.hard_violations.length + " hard violation(s)</span>";
      html += "</div>";
      if(cp.top_locations && cp.top_locations.length){
        html += "<div class='hotspot-ranked' id='cs-hotspot-ranked'>";
        var maxCount = cp.top_locations[0].count || 1;
        cp.top_locations.forEach(function(loc){
          var pct = Math.round((loc.count / maxCount) * 100);
          html += "<div class='hotspot-row'><span class='mono hs-loc'>" + loc.location_id + "</span>" +
            "<div class='hs-track'><div class='hs-fill' style='width:" + pct + "%'></div></div>" +
            "<span class='hs-count'>" + loc.count + "×</span></div>";
        });
        html += "</div>";
      }
      if(cp.binding_detail && cp.binding_detail.length){
        html += "<div class='summary-card-title' style='margin-top:14px'><span class='msi'>report</span> Over-capacity weeks</div>";
        html += "<div class='insight-rows' id='cs-hotspot-weeks'>";
        cp.binding_detail.forEach(function(h){
          html += "<div class='insight-row clickable' data-week='" + h.week + "' title='Click to view week " + h.week + " on the line map'>" +
            "<div><strong>" + h.location_id + "</strong> · week " + h.week + " — " + h.slots_used + " used against capacity " + h.supply_capacity + " (excess " + h.excess + ")</div>" +
            "<div class='ir-sub'>" + h.activities.join(", ") + "</div></div>";
        });
        html += "</div>";
      }
      html += "</div>";
    }

    // ---- 3. scenario trade-offs, in plain language ----
    if(exp && exp.scenario_tradeoffs){
      html += "<div class='summary-card'><div class='summary-card-title'><span class='msi'>balance</span> Scenario trade-offs</div>";
      html += "<p class='tradeoff-text'>" + escapeHtml(exp.scenario_tradeoffs.narrative) + "</p></div>";
    }

    // ---- 4. activity placement insights: delayed starts + multi-night weeks (clickable) ----
    if(exp && exp.activity_placements){
      var placements = exp.activity_placements.filter(function(p){ return filteredSet.has(p.activity_id); });
      var delayed = placements.filter(function(p){ return p.delay_weeks > 0; }).sort(function(a,b){ return b.delay_weeks - a.delay_weeks; });
      var multiNight = placements.filter(function(p){ return p.max_nights_in_one_week > 1; }).sort(function(a,b){ return b.max_nights_in_one_week - a.max_nights_in_one_week; });

      if(delayed.length){
        html += "<div class='summary-card'><div class='summary-card-title'><span class='msi'>schedule</span> Started later than earliest legal week (" + delayed.length + ")</div><div class='insight-rows' id='cs-delayed'>";
        delayed.forEach(function(p){
          html += "<div class='insight-row clickable' data-open='" + p.activity_id + "'>" +
            "<div><span class='mono' style='font-weight:700'>" + p.activity_id + "</span> <span class='pill neutral'>" + p.contract_number + "</span> started wk" + p.actual_first_week +
            ", <strong>" + p.delay_weeks + " week(s)</strong> after its earliest legal week " + p.earliest_legal_week +
            (p.predecessor_activity_id ? " — gated by predecessor " + p.predecessor_activity_id : "") + "</div></div>";
        });
        html += "</div></div>";
      }

      if(multiNight.length){
        html += "<div class='summary-card'><div class='summary-card-title'><span class='msi'>nights_stay</span> Relying on multiple access-nights in one week (" + multiNight.length + ")</div><div class='insight-rows' id='cs-multinight'>";
        multiNight.forEach(function(p){
          html += "<div class='insight-row clickable' data-open='" + p.activity_id + "'>" +
            "<div><span class='mono' style='font-weight:700'>" + p.activity_id + "</span> <span class='pill neutral'>" + p.contract_number + "</span> used <strong>" + p.max_nights_in_one_week + "</strong> access-nights in week " + p.peak_week +
            (p.used_eclo ? " · ECLO" : "") + "</div></div>";
        });
        html += "</div></div>";
      }
    }

    // ---- 5. weekly access-night usage (filtered, clickable -> jumps to that week on the map) ----
    html += "<div class='summary-card'><div class='summary-card-title'><span class='msi'>calendar_view_week</span> Access nights per week (" + horizonWeeks + " weeks)</div>";
    html += "<div class='week-chart'>";
    for(var wk = 1; wk <= horizonWeeks; wk++){
      var hgt = Math.max((weeklyNights[wk] / maxWeekNights) * 100, weeklyNights[wk] > 0 ? 6 : 0);
      html += "<div class='week-bar-col' data-week='" + wk + "' title='Week " + wk + " (" + fmtDate(weekToDate(wk,0).toISOString().slice(0,10)) + ") — " + weeklyNights[wk] + " access night(s) · click to view on the line map'>" +
        "<div class='week-bar' style='height:" + hgt + "%'></div></div>";
    }
    html += "</div><div class='week-chart-labels'>";
    for(var wl = 1; wl <= horizonWeeks; wl++){
      html += "<span>" + (wl === 1 || wl % 4 === 0 ? wl : "") + "</span>";
    }
    html += "</div></div>";

    // ---- 6. per-contract digest (filtered) ----
    html += "<div class='summary-card'><div class='summary-card-title'><span class='msi'>table_chart</span> Per-contract digest</div>";
    if(!visibleContractIds.length){
      html += "<div class='empty-state'>No contracts match the current filters.</div>";
    } else {
      html += "<div style='overflow-x:auto'><table class='contract-table'><thead><tr>" +
        "<th>Contract</th><th>Priority</th><th>Activities</th><th>Access nights</th><th>ECLO</th><th>Window</th><th>Completion</th><th>Status</th>" +
        "</tr></thead><tbody id='cs-contracts'>";
      visibleContractIds.forEach(function(cid){
        var contract = DATA.contracts[cid];
        var result = sc.results[cid];
        var acts = byContract[cid];
        var nights = 0, eclo = 0, minWeek = null, maxWeek = null;
        acts.forEach(function(aid){
          var sched = sc.activities[aid];
          if(!sched) return;
          nights += sched.accesses.length;
          eclo += sched.eclo_nights;
          if(minWeek === null || sched.start_week < minWeek) minWeek = sched.start_week;
          if(maxWeek === null || sched.end_week > maxWeek) maxWeek = sched.end_week;
        });
        var windowText = minWeek === null ? "—" : ("W" + minWeek + " – W" + maxWeek);
        var overrun = result.overrun_days > 0;
        html += "<tr data-contract='" + cid + "'>" +
          "<td><div class='ct-name'><span class='ct-swatch' style='background:" + contractColor[cid] + "'></span><div><div class='mono' style='font-weight:700'>" + cid + "</div><div class='ct-desc'>" + contract.description + "</div></div></div></td>" +
          "<td>P" + contract.priority + "</td>" +
          "<td>" + acts.length + "</td>" +
          "<td>" + nights + "</td>" +
          "<td>" + eclo + "</td>" +
          "<td class='mono'>" + windowText + "</td>" +
          "<td class='mono'>" + fmtDate(result.simulated_completion_date) + "</td>" +
          "<td><span class='pill " + (overrun ? "warn" : "good") + "'>" + (overrun ? ("+" + result.overrun_days + "d") : "on schedule") + "</span></td>" +
        "</tr>";
      });
      html += "</tbody></table></div>";
    }
    html += "</div>";

    html += "</div>";
    document.getElementById("panelBody").innerHTML = html;

    document.querySelectorAll(".contract-table tbody tr").forEach(function(row){
      row.addEventListener("click", function(){
        var cid = row.dataset.contract;
        state.selectedContracts = new Set([cid]);
        renderContractList();
        updateResultCount();
        switchView("gantt");
      });
    });
    document.querySelectorAll(".insight-row.clickable").forEach(function(row){
      row.addEventListener("click", function(){ openDrawer(row.dataset.open); });
    });
    document.querySelectorAll(".insight-row[data-week]").forEach(function(row){
      row.addEventListener("click", function(){
        setWeek(parseInt(row.dataset.week, 10));
        switchView("map");
      });
    });
    document.querySelectorAll(".week-bar-col").forEach(function(col){
      col.addEventListener("click", function(){
        setWeek(parseInt(col.dataset.week, 10));
        switchView("map");
      });
    });

    // point 2: any section with more than 5 lines starts collapsed, expandable on demand
    ["cs-narrative","cs-hotspot-ranked","cs-hotspot-weeks","cs-delayed","cs-multinight"].forEach(function(id){
      applyCollapse(document.getElementById(id), ":scope > *", 5);
    });
    applyCollapse(document.getElementById("cs-contracts"), "tr", 5);
  }
  var SUMMARY_COLLAPSE_MAX = 5;
  function applyCollapse(container, itemSelector, max){
    if(!container) return;
    var items = container.querySelectorAll(itemSelector);
    if(items.length <= max) return;
    for(var i = max; i < items.length; i++) items[i].classList.add("cs-hidden");
    var hiddenCount = items.length - max;
    var expanded = false;
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cs-toggle";
    function label(){ return expanded ? "<span class='msi'>expand_less</span> Show less" : "<span class='msi'>expand_more</span> Show " + hiddenCount + " more"; }
    btn.innerHTML = label();
    btn.addEventListener("click", function(){
      expanded = !expanded;
      for(var i = max; i < items.length; i++) items[i].classList.toggle("cs-hidden", !expanded);
      btn.innerHTML = label();
    });
    container.parentNode.insertBefore(btn, container.nextSibling);
  }
  function escapeHtml(s){
    return String(s).replace(/[&<>]/g, function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;"}[c]; });
  }

  // ---------------- view switching ----------------
  function switchView(view){
    stopPlay(); // leaving (or re-entering) the map tears down its DOM — never leave the interval orphaned
    state.view = view;
    document.querySelectorAll(".view-tab").forEach(function(btn){
      btn.classList.toggle("active", btn.dataset.view === view);
    });
    if(view === "gantt"){ document.getElementById("panelBody").innerHTML = ""; renderGantt(); }
    if(view === "map"){ document.getElementById("panelBody").innerHTML = ""; renderMap(); }
    if(view === "summary"){ renderSummary(); }
  }
  function renderActiveView(){
    if(state.view === "gantt") renderGantt();
    else if(state.view === "map") renderMap();
    else if(state.view === "summary") renderSummary();
  }
  document.querySelectorAll(".view-tab").forEach(function(btn){
    btn.addEventListener("click", function(){ switchView(btn.dataset.view); });
  });

  // ---------------- search & contract buttons ----------------
  document.getElementById("searchInput").addEventListener("input", function(e){
    state.search = e.target.value.trim();
    renderActiveView(); updateResultCount();
  });
  document.getElementById("selAll").addEventListener("click", function(){
    state.selectedContracts = new Set(contractIds);
    renderContractList(); renderActiveView(); updateResultCount();
  });
  document.getElementById("selNone").addEventListener("click", function(){
    state.selectedContracts = new Set();
    renderContractList(); renderActiveView(); updateResultCount();
  });

  // ---------------- footer ----------------
  function renderFooter(){
    var end = weekToDate(horizonWeeks, 6);
    var sourceIcon = {github: "cloud_sync", local: "dns", upload: "upload_file"}[DATA.source] || "cloud_sync";
    var sourceVerb = {github: "Live from GitHub", local: "Live from local disk", upload: "Uploaded dataset"}[DATA.source] || "Live from";
    var sourceLine = sourceVerb + ": <span class='mono' style='font-size:10px'>" + (DATA.source_label || "") + "</span>" +
      (DATA.source === "upload" ? " <span class='pill neutral' style='margin-left:4px'>not polled — click Upload dataset to revert</span>" : "");
    document.getElementById("sidebarFoot").innerHTML =
      "<span class='msi'>" + sourceIcon + "</span>" +
      "<span>" + sourceLine + "<br>horizon " + fmtDateObj(horizonStart) + " → " + fmtDateObj(end) +
      " (" + horizonWeeks + " wks)<br>" + contractIds.length + " contracts · " + activityIds.length + " activities</span>";
  }

  // ---------------- data loading ----------------
  function applyMeta(meta){
    DATA.params = meta.params;
    DATA.lines = meta.lines;
    DATA.stations = meta.stations;
    DATA.sectors = meta.sectors;
    DATA.location_supply = meta.location_supply;
    DATA.contracts = meta.contracts;
    DATA.activities = meta.activities;
    contractIds = Object.keys(DATA.contracts).sort();
    activityIds = Object.keys(DATA.activities).sort();
    contractColor = {};
    contractIds.forEach(function(cid, i){ contractColor[cid] = CONTRACT_COLORS[i % CONTRACT_COLORS.length]; });
    horizonStart = new Date(DATA.params.horizon_start + "T00:00:00");
    horizonWeeks = DATA.params.horizon_weeks;
    mapGeom = null;
    if(state.selectedContracts.size === 0){
      state.selectedContracts = new Set(contractIds);
    }
    lastKnownHash = meta.data_hash;
    DATA.source = meta.source;
    DATA.source_label = meta.source_label;
  }

  function loadScenario(scenario){
    return apiGet("/api/scenario/" + scenario).then(function(payload){
      DATA.scenarios[scenario] = payload;
      return payload;
    });
  }

  function switchScenario(s){
    stopPlay();
    renderScenarioTabs();
    if(!DATA.scenarios[s]){
      document.getElementById("panelBody").innerHTML = "<div class='panel-loading'><span class='spin'></span> Solving Scenario " + s + "… (first solve after starting the server can take up to ~30s while OR-tools warms up)</div>";
    }
    loadScenario(s).then(function(){
      renderStatusLine();
      document.getElementById("panelBody").innerHTML = "";
      renderActiveView();
    }).catch(function(err){
      document.getElementById("panelBody").innerHTML = "<div class='panel-error'>Could not solve Scenario " + s + ": " + err.message + "</div>";
    });
  }

  // ---------------- onboarding (no dataset loaded yet) ----------------
  function renderOnboarding(){
    document.getElementById("panelBody").innerHTML =
      "<div class='onboarding'>" +
        "<span class='msi onboarding-icon'>dataset</span>" +
        "<h2>No dataset loaded yet</h2>" +
        "<p>Upload the 8 instance CSVs to calculate and view a live schedule, or load the repository's " +
        "sample instance to explore the app right away.</p>" +
        "<div class='onboarding-actions'>" +
          "<button class='btn primary' id='onboardUploadBtn' type='button'><span class='msi'>upload_file</span> Upload dataset</button>" +
          "<button class='btn' id='onboardSampleBtn' type='button'><span class='msi'>cloud_sync</span> Load sample data</button>" +
        "</div>" +
      "</div>";
    document.getElementById("onboardUploadBtn").addEventListener("click", openUploadModal);
    document.getElementById("onboardSampleBtn").addEventListener("click", function(){
      var btn = document.getElementById("onboardSampleBtn");
      btn.disabled = true;
      btn.innerHTML = "<span class='msi'>hourglass_top</span> Loading…";
      transitionInFlight = true;
      apiPost2("/api/source", {source: "github"}).then(function(){
        toast("Sample data loaded.");
        return apiGet("/api/meta");
      }).then(function(meta){
        return enterConfigured(meta);
      }).catch(function(err){
        toast("Could not load sample data: " + err.message, true);
        btn.disabled = false;
        btn.innerHTML = "<span class='msi'>cloud_sync</span> Load sample data";
        transitionInFlight = false;
      });
    });
  }

  function enterUnconfigured(){
    state.configured = false;
    state.booted = false;
    DATA.scenarios = {};
    DATA.source = "none";
    DATA.source_label = "No dataset loaded yet";
    lastKnownHash = null;
    document.getElementById("resetBtn").hidden = true;
    document.getElementById("exportBtn").hidden = true;
    document.getElementById("scenarioTabs").innerHTML = "";
    document.getElementById("statusLine").innerHTML = "";
    document.getElementById("lineFilter").innerHTML = "";
    document.getElementById("contractList").innerHTML = "";
    document.getElementById("sidebarFoot").innerHTML = "";
    document.getElementById("resultCount").textContent = "";
    renderOnboarding();
  }

  var transitionInFlight = false;

  function enterConfigured(meta){
    transitionInFlight = true;
    applyMeta(meta);
    renderLineFilter();
    renderContractList();
    renderFooter();
    renderScenarioTabs();
    document.getElementById("resetBtn").hidden = false;
    document.getElementById("exportBtn").hidden = false;
    return loadScenario(state.scenario).then(function(){
      state.configured = true;
      state.booted = true;
      renderStatusLine();
      document.getElementById("panelBody").innerHTML = "";
      renderActiveView();
    }).finally(function(){ transitionInFlight = false; });
  }

  function boot(){
    apiGet("/api/meta").then(function(meta){
      if(meta.unconfigured){
        enterUnconfigured();
        return;
      }
      return enterConfigured(meta);
    }).then(function(){
      startStatusPolling();
    }).catch(function(err){
      document.getElementById("panelBody").innerHTML =
        "<div class='panel-error'>Could not reach the Track Access Console server: " + err.message +
        "<br>Start it with <code>python app/server.py</code> from the repo root.</div>";
    });
  }

  // ---------------- live data polling (auto-reschedule on change, cross-client state sync) ----------------
  function startStatusPolling(){
    setInterval(function(){
      if(transitionInFlight) return; // a foreground action (upload/reset/load-sample) is already handling this
      apiGet("/api/status").then(function(st){
        if(st.unconfigured){
          if(state.configured){
            toast("Dataset was reset — waiting for a new upload.");
            enterUnconfigured();
          }
          return;
        }
        if(!state.configured){
          toast("A dataset is now available — loading…");
          apiGet("/api/meta").then(function(meta){ return enterConfigured(meta); });
          return;
        }
        if(lastKnownHash && st.data_hash !== lastKnownHash){
          lastKnownHash = st.data_hash;
          toast("Instance data changed — rescheduling…");
          apiGet("/api/meta").then(function(meta){ return enterConfigured(meta); }).then(function(){
            toast("Rescheduled with updated data.");
          }).catch(function(err){
            toast("Auto-reschedule failed: " + err.message, true);
          });
        }
      }).catch(function(){ /* transient — try again next tick */ });
    }, 5000);
  }

  boot();
})();

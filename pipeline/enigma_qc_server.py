#!/usr/bin/env python3
"""
ENIGMA Lesion QC Web Interface

Supports two modes:
  - brain_extraction: Review brain extraction masks
  - registration:     Review warped T1, lesion overlay, Jacobian (default)

Usage:
    python enigma_qc_server.py --qc-dir /data/QC --mode registration --port 8890
    python enigma_qc_server.py --qc-dir /data/QC --mode brain_extraction --port 8890

Then SSH tunnel:
    ssh -L 8890:localhost:8890 user@server
    Open: http://localhost:8890
"""

import os
import sys
import json
import shutil
import hashlib
import argparse
import sqlite3
import mimetypes
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

# =============================================================================
# Configuration — set via command-line args
# =============================================================================

QC_DIR = None
DB_PATH = None
MODE = "registration"

QC_MODES = {
    "brain_extraction": {
        "title": "Brain Extraction QC",
        "types": {
            "brain_extraction": {
                "name": "Brain Extraction",
                "path": "brain_extraction",
                "pattern": "{subject}_brain_extract.png",
                "description": "T1 with brain mask edge overlay"
            }
        }
    },
    "registration": {
        "title": "Lesion Registration QC",
        "types": {
            "T1_brain": {
                "name": "T1 Brain (native)",
                "path": "T1_brain",
                "pattern": "{subject}_T1_brain.png",
                "description": "Brain-extracted T1 in native space"
            },
            "T1_lesion_overlay": {
                "name": "T1 + Lesion Overlay",
                "path": "T1_lesion_overlay",
                "pattern": "{subject}_T1_lesion.png",
                "description": "T1 with lesion mask overlaid"
            },
            "T1_warped": {
                "name": "T1 Warped (registered)",
                "path": "T1_warped",
                "pattern": "{subject}_T1_warped.png",
                "description": "T1 registered to template"
            },
            "logjacobian": {
                "name": "Log Jacobian",
                "path": "logjacobian",
                "pattern": "{subject}_logjacobian.png",
                "description": "Log Jacobian from registration"
            }
        }
    }
}


def get_qc_types():
    return QC_MODES[MODE]["types"]


def get_qc_title():
    return QC_MODES[MODE]["title"]


# =============================================================================
# Database
# =============================================================================

def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 30-second busy timeout prevents transient "database is locked" errors
    # under concurrent access (e.g. multiple browser tabs).
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL mode is more concurrency-friendly than the default rollback journal
    # and significantly reduces lock contention. Safe on local disk; not
    # recommended over NFS, which is exactly why we default to local disk.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.OperationalError:
        # On some filesystems WAL is rejected; fall back silently to default
        pass
    return conn


def init_database():
    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS subjects (
            subject_id TEXT PRIMARY KEY,
            date_added TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS qc (
            subject_id TEXT,
            qc_type TEXT,
            qc_status TEXT DEFAULT 'pending',
            qc_image_exists INTEGER DEFAULT 0,
            qc_image_path TEXT,
            qc_date TEXT,
            reviewer TEXT,
            notes TEXT,
            PRIMARY KEY (subject_id, qc_type),
            FOREIGN KEY (subject_id) REFERENCES subjects(subject_id)
        )
    ''')
    conn.commit()
    conn.close()


def scan_subjects():
    init_database()
    conn = get_connection()
    c = conn.cursor()
    scan_time = datetime.now().isoformat()
    subjects_found = set()

    for qc_type, config in get_qc_types().items():
        qc_path = QC_DIR / config["path"]
        if not qc_path.exists():
            continue
        for png_file in qc_path.glob("*.png"):
            filename = png_file.stem
            suffix = config["pattern"].replace("{subject}", "").replace(".png", "")
            subject_id = filename.replace(suffix, "")
            if not subject_id:
                continue
            subjects_found.add(subject_id)
            c.execute(
                "INSERT OR IGNORE INTO subjects (subject_id, date_added) VALUES (?, ?)",
                (subject_id, scan_time),
            )
            c.execute(
                """INSERT INTO qc (subject_id, qc_type, qc_image_exists, qc_image_path)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(subject_id, qc_type) DO UPDATE SET
                       qc_image_exists = 1, qc_image_path = excluded.qc_image_path""",
                (subject_id, qc_type, str(png_file)),
            )

    conn.commit()
    conn.close()
    return len(subjects_found)


def get_qc_summary():
    conn = get_connection()
    c = conn.cursor()
    summary = {}
    for qc_type in get_qc_types():
        counts = c.execute(
            """SELECT
                SUM(CASE WHEN qc_status = 'pending' THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN qc_status = 'pass' THEN 1 ELSE 0 END) as pass,
                SUM(CASE WHEN qc_status = 'fail' THEN 1 ELSE 0 END) as fail,
                SUM(CASE WHEN qc_status = 'review' THEN 1 ELSE 0 END) as review,
                COUNT(*) as total
            FROM qc WHERE qc_type = ? AND qc_image_exists = 1""",
            (qc_type,),
        ).fetchone()
        summary[qc_type] = dict(counts) if counts else {"pending": 0, "pass": 0, "fail": 0, "review": 0, "total": 0}
    conn.close()
    return summary


def get_qc_list(qc_type=None, status=None):
    conn = get_connection()
    c = conn.cursor()
    query = "SELECT * FROM qc WHERE qc_image_exists = 1"
    params = []
    if qc_type:
        query += " AND qc_type = ?"
        params.append(qc_type)
    if status:
        query += " AND qc_status = ?"
        params.append(status)
    query += " ORDER BY subject_id, qc_type"
    results = c.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in results]


def update_qc_status(subject_id, qc_type, status, notes=None):
    conn = get_connection()
    c = conn.cursor()
    reviewer = os.environ.get("USER", "unknown")
    c.execute(
        "UPDATE qc SET qc_status = ?, qc_date = ?, reviewer = ?, notes = ? WHERE subject_id = ? AND qc_type = ?",
        (status, datetime.now().isoformat(), reviewer, notes, subject_id, qc_type),
    )
    conn.commit()
    conn.close()
    return {"success": True, "subject_id": subject_id, "qc_type": qc_type, "status": status}


def get_subject_images(subject_id):
    conn = get_connection()
    c = conn.cursor()
    results = c.execute(
        "SELECT * FROM qc WHERE subject_id = ? AND qc_image_exists = 1 ORDER BY qc_type",
        (subject_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in results]


def export_qc_results(status=None):
    """Export QC results as TSV for downstream filtering."""
    conn = get_connection()
    c = conn.cursor()
    query = "SELECT subject_id, qc_type, qc_status, reviewer, qc_date, notes FROM qc WHERE qc_image_exists = 1"
    params = []
    if status:
        query += " AND qc_status = ?"
        params.append(status)
    query += " ORDER BY subject_id, qc_type"
    results = c.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in results]


# =============================================================================
# HTML Template (same UI, now with dynamic title and types)
# =============================================================================

def get_html():
    qc_types = get_qc_types()
    title = get_qc_title()

    type_options_html = ""
    for key, cfg in qc_types.items():
        type_options_html += f'<option value="{key}">{cfg["name"]}</option>\n'

    type_names_js = json.dumps({k: v["name"] for k, v in qc_types.items()})
    type_keys_js = json.dumps(list(qc_types.keys()))

    # Number of columns in image grid
    ncols = min(len(qc_types), 2)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            margin: 0; padding: 20px; background: #f5f5f5;
        }}
        .container {{ max-width: 1800px; margin: 0 auto; }}
        h1 {{ color: #2c3e50; margin-bottom: 3px; font-size: 22px; }}
        .subtitle {{ color: #7f8c8d; margin-bottom: 12px; font-size: 13px; }}
        .tabs {{ display: flex; gap: 5px; margin-bottom: 10px; border-bottom: 2px solid #9b59b6; padding-bottom: 5px; }}
        .tab {{ padding: 6px 14px; background: #ecf0f1; border: none; border-radius: 5px 5px 0 0; cursor: pointer; font-size: 13px; }}
        .tab:hover {{ background: #d5dbdb; }}
        .tab.active {{ background: #9b59b6; color: white; }}
        .panel {{ display: none; }}
        .panel.active {{ display: block; }}
        .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px; }}
        .summary-card {{ background: white; padding: 15px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .summary-card h3 {{ margin: 0 0 10px 0; color: #2c3e50; font-size: 14px; }}
        .summary-card .number {{ font-size: 28px; font-weight: bold; color: #9b59b6; }}
        .filters {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 10px; padding: 8px 12px; background: white; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); font-size: 13px; }}
        .filters label {{ font-weight: 500; }}
        .filters select, .filters input {{ padding: 5px 8px; border: 1px solid #ddd; border-radius: 4px; font-size: 13px; }}
        .filters button {{ padding: 5px 12px; background: #9b59b6; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 13px; }}
        .filters button:hover {{ background: #8e44ad; }}
        .badge {{ display: inline-block; padding: 4px 10px; border-radius: 4px; font-size: 12px; font-weight: 500; }}
        .badge-pending {{ background: #f39c12; color: white; }}
        .badge-pass {{ background: #27ae60; color: white; }}
        .badge-fail {{ background: #e74c3c; color: white; }}
        .badge-review {{ background: #9b59b6; color: white; }}
        .qc-container {{ display: grid; grid-template-columns: 280px 1fr; gap: 20px; height: calc(100vh - 280px); min-height: 600px; }}
        .qc-list {{ background: white; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); overflow-y: auto; }}
        .qc-list-header {{ padding: 15px; background: #ecf0f1; border-bottom: 1px solid #ddd; font-weight: 500; position: sticky; top: 0; }}
        .qc-item {{ padding: 12px 15px; border-bottom: 1px solid #ecf0f1; cursor: pointer; }}
        .qc-item:hover {{ background: #f8f9fa; }}
        .qc-item.active {{ background: #f5eef8; border-left: 4px solid #9b59b6; }}
        .qc-item .subject {{ font-weight: 600; }}
        .qc-item .info {{ font-size: 12px; color: #7f8c8d; margin-top: 4px; }}
        .qc-viewer {{ background: white; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); padding: 20px; overflow-y: auto; }}
        .qc-viewer h2 {{ margin: 0 0 8px 0; font-size: 18px; }}
        .image-grid {{ display: grid; grid-template-columns: repeat({ncols}, 1fr); gap: 10px; margin-bottom: 15px; }}
        .image-card {{ background: #1a1a1a; border-radius: 8px; overflow: hidden; }}
        .image-card-header {{ background: #2c3e50; color: white; padding: 6px 10px; font-weight: 500; font-size: 13px; }}
        .image-card-body {{ padding: 5px; display: flex; align-items: center; justify-content: center; min-height: 150px; }}
        .image-card img {{ max-width: 100%; }}
        .image-card .no-image {{ color: #666; }}
        .qc-actions {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; padding: 10px; background: #f8f9fa; border-radius: 8px; }}
        .qc-btn {{ padding: 8px 20px; border: none; border-radius: 6px; font-size: 14px; font-weight: 600; cursor: pointer; }}
        .qc-btn:hover {{ transform: translateY(-1px); box-shadow: 0 4px 8px rgba(0,0,0,0.2); }}
        .qc-btn-pass {{ background: #27ae60; color: white; }}
        .qc-btn-fail {{ background: #e74c3c; color: white; }}
        .qc-btn-review {{ background: #9b59b6; color: white; }}
        .qc-btn-skip {{ background: #95a5a6; color: white; }}
        .qc-notes {{ display: flex; gap: 8px; margin-bottom: 8px; }}
        .qc-notes input {{ flex: 1; padding: 6px 10px; border: 1px solid #ddd; border-radius: 6px; font-size: 13px; }}
        .shortcuts {{ padding: 6px 10px; background: #ecf0f1; border-radius: 6px; font-size: 12px; color: #7f8c8d; }}
        .shortcuts kbd {{ background: white; padding: 2px 6px; border-radius: 3px; border: 1px solid #bdc3c7; }}
        .toast {{ position: fixed; bottom: 20px; right: 20px; padding: 15px 25px; background: #2c3e50; color: white; border-radius: 8px; opacity: 0; transform: translateY(20px); transition: all 0.3s; z-index: 1000; }}
        .toast.show {{ opacity: 1; transform: translateY(0); }}
        .toast.success {{ background: #27ae60; }}
        .toast.error {{ background: #e74c3c; }}
        table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #ecf0f1; }}
        th {{ background: #9b59b6; color: white; }}
        tr:hover {{ background: #f8f9fa; }}
        .progress-bar {{ height: 8px; background: #ecf0f1; border-radius: 4px; overflow: hidden; }}
        .progress-bar .fill {{ height: 100%; background: #27ae60; }}
        .export-btn {{ padding: 8px 16px; background: #27ae60; color: white; border: none; border-radius: 4px; cursor: pointer; margin-left: 10px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>{title}</h1>
        <span class="subtitle">ENIGMA Brain Injury Working Group</span>

        <div class="tabs">
            <button class="tab active" onclick="showPanel('dashboard')">Dashboard</button>
            <button class="tab" onclick="showPanel('qc-review')">QC Review</button>
        </div>

        <div id="dashboard" class="panel active">
            <div class="summary-grid" id="summary-cards"></div>
            <h3>QC Progress by Type</h3>
            <table id="progress-table"><thead><tr>
                <th>QC Type</th><th>Pending</th><th>Pass</th><th>Fail</th><th>Review</th><th>Progress</th>
            </tr></thead><tbody></tbody></table>
            <br>
            <button onclick="rescanSubjects()" style="padding:10px 20px;background:#9b59b6;color:white;border:none;border-radius:4px;cursor:pointer;">Rescan Subjects</button>
            <button class="export-btn" onclick="exportResults('pass')">Export Passed Subjects</button>
            <button class="export-btn" onclick="exportResults('fail')" style="background:#e74c3c;">Export Failed Subjects</button>
            <button class="export-btn" onclick="exportResults('')" style="background:#3498db;">Export All</button>
        </div>

        <div id="qc-review" class="panel">
            <div class="filters">
                <label>Type:</label>
                <select id="filter-qc-type" onchange="loadQCList()">
                    <option value="">All Types</option>
                    {type_options_html}
                </select>
                <label>Status:</label>
                <select id="filter-status" onchange="loadQCList()">
                    <option value="">All</option>
                    <option value="pending" selected>Pending</option>
                    <option value="pass">Pass</option>
                    <option value="fail">Fail</option>
                    <option value="review">Review</option>
                </select>
                <button onclick="loadQCList()">Refresh</button>
                <label style="margin-left:16px;">Jump to:</label>
                <input id="jump-to-subject" list="jump-subject-options" type="text" placeholder="subject ID" style="padding:4px 8px;" onkeydown="if(event.key==='Enter') jumpToSubject();">
                <datalist id="jump-subject-options"></datalist>
                <button onclick="jumpToSubject()">Go</button>
            </div>
            <div class="qc-container">
                <div class="qc-list">
                    <div class="qc-list-header"><span id="qc-list-count">0</span> items</div>
                    <div id="qc-list-items"></div>
                </div>
                <div class="qc-viewer">
                    <h2 id="qc-subject-title">Select a subject to review</h2>
                    <div class="image-grid" id="image-grid"></div>
                    <div class="bottom-bar">
                        <button class="qc-btn qc-btn-pass" onclick="submitQC('pass')">Pass</button>
                        <button class="qc-btn qc-btn-fail" onclick="submitQC('fail')">Fail</button>
                        <button class="qc-btn qc-btn-review" onclick="submitQC('review')">Review</button>
                        <button class="qc-btn qc-btn-skip" onclick="nextSubject()">Skip</button>
                        <input type="text" class="notes-input" id="qc-notes" placeholder="Optional notes...">
                        <span class="shortcuts"><kbd>P</kbd> Pass <kbd>F</kbd> Fail <kbd>R</kbd> Review <kbd>N</kbd> Next</span>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <div id="toast" class="toast"></div>
    <div id="lightbox" class="lightbox" onclick="closeLightbox(event)">
        <span class="lightbox-close" onclick="closeLightbox()">&times;</span>
        <div class="lightbox-help">scroll = zoom · drag = pan · 0 reset · esc close</div>
        <div id="lightbox-wrap" class="lightbox-wrap" onclick="event.stopPropagation()">
            <img id="lightbox-img" src="" alt="Enlarged QC image" draggable="false">
        </div>
    </div>
    <style>
        .lightbox {{ display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0,0,0,0.85); z-index: 9999; cursor: pointer; align-items: center; justify-content: center; overflow: hidden; }}
        .lightbox.active {{ display: flex; }}
        .lightbox-wrap {{ width: 100vw; height: 100vh; display: flex; align-items: center; justify-content: center; cursor: grab; overflow: hidden; }}
        .lightbox-wrap.dragging {{ cursor: grabbing; }}
        .lightbox img {{ max-width: 95vw; max-height: 95vh; object-fit: contain; border-radius: 4px; transform-origin: 0 0; user-select: none; -webkit-user-drag: none; }}
        .lightbox-close {{ position: fixed; top: 15px; right: 25px; color: white; font-size: 28px; cursor: pointer; z-index: 10000; }}
        .lightbox-help {{ position: fixed; bottom: 15px; left: 50%; transform: translateX(-50%); color: rgba(255,255,255,0.7); font-size: 13px; z-index: 10000; pointer-events: none; font-family: -apple-system, sans-serif; }}
    </style>

<script>
const typeNames = {type_names_js};
const typeKeys = {type_keys_js};
let qcItems = [];
let currentIndex = -1;
let currentSubject = null;

document.addEventListener('DOMContentLoaded', function() {{
    loadDashboard();
    loadQCList().then(() => {{
        // If URL contains ?subject=X, try to jump to that subject after the
        // list has loaded.
        const params = new URLSearchParams(window.location.search);
        const wantSubject = params.get('subject');
        if (wantSubject) {{
            jumpToSubject(wantSubject);
        }}
    }});
    document.addEventListener('keydown', handleKeyboard);
}});

function jumpToSubject(subjectId) {{
    // Called either from the input box (subjectId undefined → read from input)
    // or programmatically with an explicit subject ID.
    if (subjectId === undefined) {{
        subjectId = document.getElementById('jump-to-subject').value.trim();
    }}
    if (!subjectId) return;
    // Try to find the subject in the current filtered list first
    const items = document.querySelectorAll('.qc-item');
    for (let i = 0; i < items.length; i++) {{
        if (items[i].dataset.subject === subjectId) {{
            selectSubject(subjectId, parseInt(items[i].dataset.index));
            items[i].scrollIntoView({{ block: 'center', behavior: 'smooth' }});
            return;
        }}
    }}
    // Not in current view — fall back to fetching it directly (the API
    // returns images regardless of filter status, so this still works for
    // already-rated subjects when filtered to 'pending').
    selectSubject(subjectId, -1);
    showToast("Loaded "+subjectId+" (not in current filter)", "success");
}}

function showPanel(id) {{
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.getElementById(id).classList.add('active');
    event.target.classList.add('active');
}}

async function loadDashboard() {{
    try {{
        const resp = await fetch('/api/summary');
        const data = await resp.json();
        let totalPending=0, totalPass=0, totalFail=0, totalItems=0;
        const tbody = document.querySelector('#progress-table tbody');
        tbody.innerHTML = '';
        for (const [qcType, counts] of Object.entries(data)) {{
            totalPending += counts.pending||0;
            totalPass += counts.pass||0;
            totalFail += counts.fail||0;
            totalItems += counts.total||0;
            const done = (counts.pass||0) + (counts.fail||0);
            const pct = counts.total > 0 ? Math.round(done/counts.total*100) : 0;
            tbody.innerHTML += '<tr><td><strong>'+(typeNames[qcType]||qcType)+'</strong></td>'
                +'<td>'+(counts.pending||0)+'</td><td>'+(counts.pass||0)+'</td>'
                +'<td>'+(counts.fail||0)+'</td><td>'+(counts.review||0)+'</td>'
                +'<td><div class="progress-bar"><div class="fill" style="width:'+pct+'%"></div></div>'+pct+'%</td></tr>';
        }}
        const nTypes = typeKeys.length;
        document.getElementById('summary-cards').innerHTML =
            '<div class="summary-card"><h3>Pending</h3><div class="number">'+totalPending+'</div></div>'
            +'<div class="summary-card"><h3>Passed</h3><div class="number" style="color:#27ae60">'+totalPass+'</div></div>'
            +'<div class="summary-card"><h3>Failed</h3><div class="number" style="color:#e74c3c">'+totalFail+'</div></div>'
            +'<div class="summary-card"><h3>Subjects</h3><div class="number">'+Math.round(totalItems/nTypes)+'</div></div>';
    }} catch(err) {{ console.error('Dashboard error:', err); }}
}}

async function loadQCList() {{
    try {{
        const qcType = document.getElementById('filter-qc-type').value;
        const status = document.getElementById('filter-status').value;
        const params = new URLSearchParams();
        if (qcType) params.append('qc_type', qcType);
        if (status) params.append('status', status);
        const resp = await fetch('/api/qc-list?'+params.toString());
        qcItems = await resp.json();
        const subjects = {{}};
        qcItems.forEach(item => {{
            if (!subjects[item.subject_id]) subjects[item.subject_id] = {{subject_id:item.subject_id, items:[], status:item.qc_status}};
            subjects[item.subject_id].items.push(item);
        }});
        const subjectList = Object.values(subjects);
        document.getElementById('qc-list-count').textContent = subjectList.length;
        const container = document.getElementById('qc-list-items');
        let html = '';
        subjectList.forEach((subj, idx) => {{
            const cls = idx===currentIndex?' active':'';
            html += '<div class="qc-item'+cls+'" data-subject="'+subj.subject_id+'" data-index="'+idx+'" onclick="selectSubject(this.dataset.subject,parseInt(this.dataset.index))">'
                +'<div class="subject">'+subj.subject_id+'</div>'
                +'<div class="info">'+subj.items.length+' images | <span class="badge badge-'+subj.status+'">'+subj.status+'</span></div></div>';
        }});
        container.innerHTML = html;

        // Populate the jump-to-subject autocomplete with all subjects in
        // the current filtered view.
        const dl = document.getElementById('jump-subject-options');
        if (dl) {{
            dl.innerHTML = subjectList.map(s =>
                '<option value="'+s.subject_id+'">').join('');
        }}

        if (subjectList.length > 0 && currentIndex === -1) selectSubject(subjectList[0].subject_id, 0);
    }} catch(err) {{ console.error('QC list error:', err); }}
}}

async function selectSubject(subjectId, index) {{
    currentIndex = index;
    currentSubject = subjectId;
    document.querySelectorAll('.qc-item').forEach((el,i) => el.classList.toggle('active', i===index));
    document.getElementById('qc-subject-title').textContent = subjectId;

    // Update URL so the current subject is shareable (no history entry,
    // just replaces the current state).
    try {{
        const u = new URL(window.location);
        u.searchParams.set('subject', subjectId);
        window.history.replaceState({{}}, '', u);
    }} catch (e) {{}}
    try {{
        const resp = await fetch('/api/subject-images/'+encodeURIComponent(subjectId));
        const images = await resp.json();
        let gridHtml = '';
        typeKeys.forEach(qcType => {{
            const img = images.find(i => i.qc_type === qcType);
            const imgSrc = img && img.qc_image_path ? '/image?path='+encodeURIComponent(img.qc_image_path) : '';
            gridHtml += '<div class="image-card"><div class="image-card-header">'+typeNames[qcType]+'</div>'
                +'<div class="image-card-body"'+(imgSrc ? ' data-src="'+imgSrc+'" onclick="openLightbox(this.dataset.src)"' : '')+'>'
                +(imgSrc ? '<img src="'+imgSrc+'" />' : '<span class="no-image">No image</span>')
                +'</div></div>';
        }});
        document.getElementById('image-grid').innerHTML = gridHtml;
    }} catch(err) {{ console.error('Image load error:', err); }}
    document.getElementById('qc-notes').value = '';
}}

async function submitQC(status) {{
    if (!currentSubject) {{ showToast('No subject selected','error'); return; }}
    const notes = document.getElementById('qc-notes').value;
    try {{
        for (const qcType of typeKeys) {{
            await fetch('/api/qc-update', {{
                method:'POST', headers:{{'Content-Type':'application/json'}},
                body: JSON.stringify({{subject_id:currentSubject, qc_type:qcType, status:status, notes:notes}})
            }});
        }}
        showToast(currentSubject+' → '+status, 'success');
        nextSubject();
        if (document.getElementById('filter-status').value === 'pending') loadQCList();
    }} catch(err) {{ showToast('Error: '+err.message,'error'); }}
}}

function nextSubject() {{
    const items = document.querySelectorAll('.qc-item');
    if (currentIndex < items.length-1) items[currentIndex+1].click();
    else showToast('End of list!','success');
}}

function prevSubject() {{
    if (currentIndex > 0) document.querySelectorAll('.qc-item')[currentIndex-1].click();
}}

function handleKeyboard(e) {{
    if (!document.getElementById('qc-review').classList.contains('active')) return;
    if (e.target.tagName === 'INPUT') return;
    switch(e.key.toLowerCase()) {{
        case 'p': submitQC('pass'); break;
        case 'f': submitQC('fail'); break;
        case 'r': submitQC('review'); break;
        case 'n': nextSubject(); break;
        case 'arrowdown': e.preventDefault(); nextSubject(); break;
        case 'arrowup': e.preventDefault(); prevSubject(); break;
    }}
}}

async function rescanSubjects() {{
    showToast('Scanning...','');
    try {{
        const resp = await fetch('/api/scan', {{method:'POST'}});
        const data = await resp.json();
        showToast('Found '+data.count+' subjects','success');
        loadDashboard(); loadQCList();
    }} catch(err) {{ showToast('Scan failed','error'); }}
}}

async function exportResults(status) {{
    const params = status ? '?status='+status : '';
    try {{
        const resp = await fetch('/api/export'+params);
        const data = await resp.json();
        let tsv = 'subject_id\\tqc_type\\tqc_status\\treviewer\\tqc_date\\tnotes\\n';
        data.forEach(r => {{
            tsv += [r.subject_id, r.qc_type, r.qc_status, r.reviewer||'', r.qc_date||'', r.notes||''].join('\\t')+'\\n';
        }});
        const blob = new Blob([tsv], {{type:'text/tab-separated-values'}});
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'qc_results'+(status ? '_'+status : '')+'.tsv';
        a.click();
        URL.revokeObjectURL(url);
    }} catch(err) {{ showToast('Export failed','error'); }}
}}

// --- Lightbox with zoom + pan ---------------------------------------------
let lbZoom = 1, lbPanX = 0, lbPanY = 0;
let lbDragging = false, lbStartX = 0, lbStartY = 0;

function applyLightboxTransform() {{
    const img = document.getElementById('lightbox-img');
    img.style.transform = `translate(${{lbPanX}}px, ${{lbPanY}}px) scale(${{lbZoom}})`;
}}

function resetLightbox() {{
    lbZoom = 1; lbPanX = 0; lbPanY = 0;
    applyLightboxTransform();
}}

function openLightbox(src) {{
    document.getElementById('lightbox-img').src = src;
    document.getElementById('lightbox').classList.add('active');
    resetLightbox();
}}

function closeLightbox(e) {{
    // Only close on background click, not drag-end on the wrap
    if (e && e.target && e.target.id !== 'lightbox') return;
    document.getElementById('lightbox').classList.remove('active');
    document.getElementById('lightbox-img').src = '';
    resetLightbox();
}}

(function setupLightboxInteraction() {{
    const wrap = document.getElementById('lightbox-wrap');
    if (!wrap) return;

    wrap.addEventListener('wheel', function(e) {{
        if (!document.getElementById('lightbox').classList.contains('active')) return;
        e.preventDefault();
        // Zoom around cursor position
        const rect = wrap.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;
        const factor = e.deltaY < 0 ? 1.15 : 1/1.15;
        const newZoom = Math.max(0.25, Math.min(10, lbZoom * factor));
        // Adjust pan so the point under the cursor stays put
        lbPanX = mx - (mx - lbPanX) * (newZoom / lbZoom);
        lbPanY = my - (my - lbPanY) * (newZoom / lbZoom);
        lbZoom = newZoom;
        applyLightboxTransform();
    }}, {{ passive: false }});

    wrap.addEventListener('mousedown', function(e) {{
        lbDragging = true;
        lbStartX = e.clientX - lbPanX;
        lbStartY = e.clientY - lbPanY;
        wrap.classList.add('dragging');
        e.preventDefault();
    }});
    window.addEventListener('mousemove', function(e) {{
        if (!lbDragging) return;
        lbPanX = e.clientX - lbStartX;
        lbPanY = e.clientY - lbStartY;
        applyLightboxTransform();
    }});
    window.addEventListener('mouseup', function() {{
        if (lbDragging) {{
            lbDragging = false;
            wrap.classList.remove('dragging');
        }}
    }});
}})();

document.addEventListener('keydown', function(e) {{
    const lbActive = document.getElementById('lightbox').classList.contains('active');
    if (!lbActive) return;
    if (e.key === 'Escape') closeLightbox();
    else if (e.key === '0') resetLightbox();
    else if (e.key === '+' || e.key === '=') {{ lbZoom = Math.min(10, lbZoom * 1.15); applyLightboxTransform(); }}
    else if (e.key === '-' || e.key === '_') {{ lbZoom = Math.max(0.25, lbZoom / 1.15); applyLightboxTransform(); }}
}});

function showToast(msg, type) {{
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast show '+(type||'');
    setTimeout(function(){{ t.classList.remove('show'); }}, 3000);
}}
</script>
</body>
</html>'''


# =============================================================================
# HTTP Handler
# =============================================================================

class QCRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def send_html(self, html):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def send_image(self, path):
        try:
            p = Path(path)
            if not p.exists():
                self.send_error(404)
                return
            mime_type, _ = mimetypes.guess_type(str(p))
            content = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime_type or "image/png")
            self.send_header("Content-Length", len(content))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, str(e))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self.send_html(get_html())
        elif path == "/api/summary":
            self.send_json(get_qc_summary())
        elif path == "/api/qc-list":
            self.send_json(get_qc_list(
                query.get("qc_type", [None])[0],
                query.get("status", [None])[0],
            ))
        elif path.startswith("/api/subject-images/"):
            subject_id = unquote(path.split("/api/subject-images/")[1])
            self.send_json(get_subject_images(subject_id))
        elif path == "/api/export":
            status = query.get("status", [None])[0]
            self.send_json(export_qc_results(status))
        elif path == "/image":
            img_path = query.get("path", [None])[0]
            if img_path:
                self.send_image(unquote(img_path))
            else:
                self.send_error(400)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body) if body else {}
        except Exception:
            self.send_json({"error": "Invalid JSON"}, 400)
            return

        if path == "/api/scan":
            self.send_json({"success": True, "count": scan_subjects()})
        elif path == "/api/qc-update":
            self.send_json(update_qc_status(
                data.get("subject_id"),
                data.get("qc_type"),
                data.get("status"),
                data.get("notes"),
            ))
        else:
            self.send_error(404)


# =============================================================================
# Main
# =============================================================================

def run_server(port=8890):
    init_database()
    count = scan_subjects()
    server = HTTPServer(("", port), QCRequestHandler)

    print("")
    print("=" * 60)
    print(f"  {get_qc_title()}")
    print("=" * 60)
    print(f"  Mode:         {MODE}")
    print(f"  QC directory: {QC_DIR}")
    print(f"  Database:     {DB_PATH}")
    print(f"  Subjects:     {count}")
    print(f"  Local URL:    http://localhost:{port}")
    print("")
    print(f"  SSH tunnel:   ssh -L {port}:localhost:{port} user@server")
    print(f"  Then open:    http://localhost:{port}")
    print("")
    print("  Press Ctrl+C to stop")
    print("=" * 60)
    print("")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


def default_db_path_for(qc_dir: Path) -> Path:
    """Pick a per-QC-directory database path on local disk.

    Default: ~/.cache/enigma-lesion-qc/qc_<hash>.db, where <hash> is a short
    digest of the absolute QC directory path. This keeps one DB per QC
    directory while avoiding SQLite locking issues on network filesystems
    (NFS, CIFS, Lustre, etc.) where the QC images are commonly stored.
    """
    qc_abs = str(qc_dir.resolve())
    digest = hashlib.sha1(qc_abs.encode("utf-8")).hexdigest()[:12]
    cache_root = Path.home() / ".cache" / "enigma-lesion-qc"
    return cache_root / f"qc_{digest}.db"


def migrate_legacy_db(qc_dir: Path, new_db: Path) -> None:
    """If a legacy in-QC-dir database exists, copy it to the new location.

    The previous version of this server kept the SQLite database next to
    the QC images inside the QC directory itself, which caused 'database
    is locked' errors on network filesystems. On first launch after
    upgrading, we copy that legacy DB to the new local-disk location so
    prior QC ratings are preserved. The legacy file is left in place
    (not deleted) so users can restore it if anything looks off.
    """
    legacy = qc_dir / "enigma_lesion_qc.db"
    if not legacy.exists():
        return
    if new_db.exists():
        # New DB already exists — don't overwrite it. The user has been
        # using the new location already.
        return
    new_db.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(str(legacy), str(new_db))
        print(f"  Migrated legacy QC database:")
        print(f"    from: {legacy}")
        print(f"      to: {new_db}")
        print(f"    (legacy file kept in place; you can delete it once you've")
        print(f"     verified your QC ratings are intact)")
    except (OSError, shutil.Error) as e:
        print(f"  WARNING: could not migrate legacy DB ({e}); starting fresh.")


def main():
    global QC_DIR, DB_PATH, MODE

    parser = argparse.ArgumentParser(description="ENIGMA Lesion QC Server")
    parser.add_argument("--port", "-p", type=int, default=8890)
    parser.add_argument("--qc-dir", required=True, help="Path to QC directory")
    parser.add_argument("--mode", choices=["brain_extraction", "registration"],
                        default="registration", help="QC mode (default: registration)")
    parser.add_argument(
        "--db-path",
        default=None,
        help=("Path to the SQLite QC database. Defaults to "
              "~/.cache/enigma-lesion-qc/qc_<hash>.db on local disk to "
              "avoid network-filesystem locking issues. Specify a custom "
              "path here for multi-reviewer setups on a working network "
              "filesystem (read your filesystem's SQLite/lockd docs first)."))
    args = parser.parse_args()

    QC_DIR = Path(args.qc_dir).resolve()
    MODE = args.mode

    if args.db_path:
        DB_PATH = Path(args.db_path).resolve()
    else:
        DB_PATH = default_db_path_for(QC_DIR)
        # First-launch-after-upgrade: copy old in-QC-dir DB if present.
        migrate_legacy_db(QC_DIR, DB_PATH)

    run_server(args.port)


if __name__ == "__main__":
    main()

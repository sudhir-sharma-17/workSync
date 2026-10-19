"use client";

import React, { useState, useEffect, useMemo, useRef, useCallback } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import {
  CheckCircle,
  AlertTriangle,
  Play,
  Pause,
  X,
  ChevronRight,
  Loader2,
  Clock,
  Search,
  Filter,
  Edit2,
  ChevronLeft,
  Square,
  Check,
  XCircle,
  FileDown,
  ShieldCheck,
  ShieldAlert,
  Zap,
  GitCompare,
  Maximize2,
} from "lucide-react";
import jsPDF from "jspdf";
import autoTable from "jspdf-autotable";

// ── Types ────────────────────────────────────────────────────────────────────

interface PreviewRecord {
  id: string;
  attendance_date: string;
  worker_name: string;
  worker_type?: string;
  project_name: string;
  boq_category: string;
  duration: string;
  description: string;
  status: "valid" | "warning" | "invalid";
  error_message?: string;
}

interface ValidationReport {
  passed: boolean;
  form_url: string;
  fields_detected: number;
  found: string[];
  missing: string[];
  message: string;
}

interface BatchStatus {
  batch_status: string;
  total: number;
  submitted: number;
  failed: number;
  needs_review?: number;
  uncertain?: number;
  dry_run_verified?: number;
  pending: number;
  progress_pct: number;
  logs: string[];
}

import { getApiUrl } from "@/lib/api";
const API_URL = getApiUrl();

function getSessionId(): string {
  if (typeof window === "undefined") return "";
  return sessionStorage.getItem("session_id") || "";
}

function sessionHeaders(): Record<string, string> {
  const session_id = getSessionId();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (session_id) {
    headers["X-Session-ID"] = session_id;
  }
  return headers;
}

const FIELD_LABELS: Record<string, string> = {
  attendance_date: "Attendance Date",
  worker_name: "Worker Name",
  project_name: "Project Name",
  boq_category: "BOQ Category",
  duration: "Duration",
  description: "Description",
};

// ── Component ────────────────────────────────────────────────────────────────

export default function PreviewPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const batchId = searchParams.get("batch_id") || "";

  // Data
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [records, setRecords] = useState<PreviewRecord[]>([]);
  const [formUrl, setFormUrl] = useState("");
  const [debugMeta, setDebugMeta] = useState<any>(null);

  // Validation
  const [validating, setValidating] = useState(false);
  const [validationReport, setValidationReport] = useState<ValidationReport | null>(null);

  // Submission flow
  type SubmissionMode = "count_only" | "dry_run" | "test" | "production";
  const [mode, setMode] = useState<SubmissionMode>("test");
  type FlowStatus = "idle" | "validated" | "submitting" | "completed" | "cancelled" | "failed";
  const [flowStatus, setFlowStatus] = useState<FlowStatus>("idle");

  // Live monitor state (from real API polling)
  const [liveStatus, setLiveStatus] = useState<BatchStatus | null>(null);
  const pollRef = useRef<NodeJS.Timeout | null>(null);

  // Table controls
  const [searchQuery, setSearchQuery] = useState("");
  const [filterStatus, setFilterStatus] = useState("all");
  const [currentPage, setCurrentPage] = useState(1);
  const recordsPerPage = 10;

  // Edit modal
  const [editingRecord, setEditingRecord] = useState<PreviewRecord | null>(null);
  const [isMonitorExpanded, setIsMonitorExpanded] = useState(false);

  // Project resolution states
  const [resolutionSummaryOpen, setResolutionSummaryOpen] = useState(true);
  const [projectCatalog, setProjectCatalog] = useState<string[]>([]);

  // Worker resolution states
  const [workerResolutionSummaryOpen, setWorkerResolutionSummaryOpen] = useState(true);
  const [workerCatalog, setWorkerCatalog] = useState<string[]>([]);

  // ── Polling live status ──────────────────────────────────────────────────
  const startPolling = useCallback(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const res = await fetch(`${API_URL}/api/automation/status/${batchId}`, {
          headers: sessionHeaders(),
        });
        if (!res.ok) return;
        const data: BatchStatus = await res.json();
        setLiveStatus(data);

        const done = ["Completed", "Failed", "Cancelled"].includes(data.batch_status);
        if (done) {
          clearInterval(pollRef.current!);
          setFlowStatus(
            data.batch_status === "Completed" ? "completed" :
            data.batch_status === "Cancelled" ? "cancelled" : "failed"
          );
        }
      } catch {}
    }, 2000);
  }, [batchId]);

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  // ── Load preview data ────────────────────────────────────────────────────
  useEffect(() => {
    const loadData = async () => {
      if (!batchId) {
        setLoadError("No batch ID provided. Please upload an attendance sheet first.");
        setLoading(false);
        return;
      }
      try {
        const res = await fetch(`${API_URL}/api/records/preview/${batchId}`, {
          headers: sessionHeaders(),
        });
        if (res.ok) {
          const data = await res.json();
          setRecords(data.records || []);
          setFormUrl(data.form_url || "");
          setDebugMeta(data.debug_meta || null);

          // Fetch project catalog
          try {
            const catalogRes = await fetch(`${API_URL}/api/records/project-catalog/${batchId}`, {
              headers: sessionHeaders(),
            });
            if (catalogRes.ok) {
              const catalogData = await catalogRes.json();
              const rawCatalog = catalogData.catalog || [];
              const durationPatterns = [/^\d+-\d+\s*hours?$/i, /^ot\s*\(/i];
              const filtered = rawCatalog.filter((p: string) => !durationPatterns.some((pat) => pat.test(p)));
              setProjectCatalog(filtered);
            }
          } catch (e) {
            console.error("Failed to load project catalog:", e);
          }

          // Fetch worker catalog
          try {
            const wCatalogRes = await fetch(`${API_URL}/api/records/worker-catalog/${batchId}`, {
              headers: sessionHeaders(),
            });
            if (wCatalogRes.ok) {
              const wCatalogData = await wCatalogRes.json();
              const wList: string[] = wCatalogData.catalog || [];
              if (!wList.some((w: string) => w.toLowerCase() === "carpenter_rambrid")) {
                wList.push("Carpenter_rambrid");
              }
              setWorkerCatalog(wList);
            }
          } catch (e) {

            console.error("Failed to load worker catalog:", e);
          }

          // Restore submission flow state if already running, completed, etc.
          const statusRes = await fetch(`${API_URL}/api/automation/status/${batchId}`, {
            headers: sessionHeaders(),
          });
          if (statusRes.ok) {
            const statusData: BatchStatus = await statusRes.json();
            setLiveStatus(statusData);
            if (statusData.batch_status === "Running" || statusData.batch_status === "Paused") {
              setFlowStatus("submitting");
              startPolling();
            } else if (statusData.batch_status === "Completed") {
              setFlowStatus("completed");
            } else if (statusData.batch_status === "Cancelled") {
              setFlowStatus("cancelled");
            } else if (statusData.batch_status === "Failed") {
              setFlowStatus("failed");
            }
          }
        } else {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `Server returned ${res.status}`);
        }
      } catch (err: any) {
        setLoadError(err?.message || "Failed to load preview data.");
      } finally {
        setLoading(false);
      }
    };
    loadData();
  }, [batchId, startPolling]);

  // ── Derived data ─────────────────────────────────────────────────────────
  const validRecords = useMemo(() => records.filter((r) => r.status !== "invalid"), [records]);

  const filteredRecords = useMemo(() =>
    records.filter((r) => {
      const matchesSearch =
        r.worker_name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        r.project_name.toLowerCase().includes(searchQuery.toLowerCase());
      const matchesStatus = filterStatus === "all" || r.status === filterStatus;
      return matchesSearch && matchesStatus;
    }), [records, searchQuery, filterStatus]);

  const totalPages = Math.ceil(filteredRecords.length / recordsPerPage);
  const paginatedRecords = filteredRecords.slice(
    (currentPage - 1) * recordsPerPage,
    currentPage * recordsPerPage
  );

  const durationOptions = [
    "0-2 hours",
    "2-4 hours",
    "4-6 hours",
    "6-8 hours",
    "8-10 hours",
    "OT (2 Hours)",
    "OT (4 Hours)"
  ];

  // ── Actions ───────────────────────────────────────────────────────────────

  const handleValidateForm = async () => {
    if (!formUrl) return;
    setValidating(true);
    setValidationReport(null);
    try {
      const res = await fetch(`${API_URL}/api/automation/validate`, {
        method: "POST",
        headers: sessionHeaders(),
        body: JSON.stringify({ form_url: formUrl }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || "Validation failed");
      }
      setValidationReport(data);
      if (data.passed) setFlowStatus("validated");
    } catch (err: any) {
      setValidationReport({
        passed: false,
        form_url: formUrl,
        fields_detected: 0,
        found: [],
        missing: [],
        message: err?.message || "Network error",
      });
    } finally {
      setValidating(false);
    }
  };

  const handleStartSubmission = async () => {
    setFlowStatus("submitting");
    setLiveStatus(null);
    try {
      const res = await fetch(`${API_URL}/api/automation/run`, {
        method: "POST",
        headers: sessionHeaders(),
        body: JSON.stringify({ batch_id: batchId, mode }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to start submission.");
      }
      if (mode === "count_only") {
        const data = await res.json();
        setLiveStatus({
          batch_status: "Completed",
          total: data.would_submit,
          submitted: 0,
          failed: 0,
          needs_review: 0,
          pending: data.would_submit,
          progress_pct: 100,
          logs: [`[COUNT ONLY]: ${data.would_submit} pending records ready for submission. No browser opened.`]
        });
        setFlowStatus("completed");
      } else {
        startPolling();
      }
    } catch (err: any) {
      setFlowStatus("failed");
    }
  };

  const handlePause = async () => {
    await fetch(`${API_URL}/api/automation/pause/${batchId}`, { method: "POST", headers: sessionHeaders() });
    if (pollRef.current) clearInterval(pollRef.current);
  };

  const handleCancel = async () => {
    await fetch(`${API_URL}/api/automation/cancel/${batchId}`, { method: "POST", headers: sessionHeaders() });
    if (pollRef.current) clearInterval(pollRef.current);
    setFlowStatus("cancelled");
  };

  const handleSaveEdit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (editingRecord) {
      try {
        const res = await fetch(`${API_URL}/api/records/attendance/${editingRecord.id}`, {
          method: "PUT",
          headers: sessionHeaders(),
          body: JSON.stringify({
            project_name: editingRecord.project_name,
            worker_name: editingRecord.worker_name,
            description: editingRecord.description,
            duration: editingRecord.duration
          })
        });
        if (res.ok) {
          setRecords(records.map((r) => (r.id === editingRecord.id ? editingRecord : r)));
          setEditingRecord(null);
        }
      } catch (err) {
        console.error("Error saving record edit:", err);
      }
    }
  };

  const handleAliasMappingCorrection = async (inputProject: string, newProject: string) => {
    if (!newProject) return;

    // Determine previous resolved project for matching
    const prevResolved = debugMeta?.project_resolution?.find(
      (r: any) => (r.input_project || "").toLowerCase() === inputProject.toLowerCase()
    )?.resolved_project;

    // 1. Immediate UI state update (optimistic update)
    setDebugMeta((prev: any) => {
      if (!prev) return prev;
      const currentResolutions = prev.project_resolution || [];
      let found = false;
      const updatedResolutions = currentResolutions.map((r: any) => {
        if ((r.input_project || "").toLowerCase() === inputProject.toLowerCase()) {
          found = true;
          return {
            ...r,
            resolved_project: newProject,
            confidence: 100,
            status: "Auto-Accepted",
            match_type: "Manual Selection",
          };
        }
        return r;
      });
      if (!found) {
        updatedResolutions.push({
          input_project: inputProject,
          resolved_project: newProject,
          confidence: 100,
          status: "Auto-Accepted",
          match_type: "Manual Selection",
        });
      }
      return { ...prev, project_resolution: updatedResolutions };
    });

    // Immediately update preview records in UI state
    setRecords((prevRecords) =>
      prevRecords.map((rec) => {
        const curProj = (rec.project_name || "").toLowerCase();
        const matchesInput = curProj === inputProject.toLowerCase();
        const matchesPrev = prevResolved && curProj === prevResolved.toLowerCase();
        if (matchesInput || matchesPrev) {
          return { ...rec, project_name: newProject };
        }
        return rec;
      })
    );

    // 2. Persist canonical mapping to backend (updates AttendanceRecord, debug_meta, ProjectAlias)
    try {
      const res = await fetch(`${API_URL}/api/records/project-alias`, {
        method: "POST",
        headers: sessionHeaders(),
        body: JSON.stringify({
          batch_id: batchId,
          input_project: inputProject,
          resolved_project: newProject,
        }),
      });
      if (res.ok) {
        const refreshRes = await fetch(`${API_URL}/api/records/preview/${batchId}`, {
          headers: sessionHeaders(),
        });
        if (refreshRes.ok) {
          const data = await refreshRes.json();
          setRecords(data.records || []);
          setDebugMeta(data.debug_meta || null);
        }
      }
    } catch (e) {
      console.error("Failed to save project alias:", e);
    }
  };

  const handleWorkerAliasMappingCorrection = async (inputWorker: string, resolvedWorker: string) => {
    if (!resolvedWorker) return;
    try {
      const res = await fetch(`${API_URL}/api/records/worker-alias`, {
        method: "POST",
        headers: sessionHeaders(),
        body: JSON.stringify({
          batch_id: batchId,
          input_worker: inputWorker,
          resolved_worker: resolvedWorker
        })
      });
      if (res.ok) {
        const refreshRes = await fetch(`${API_URL}/api/records/preview/${batchId}`, {
          headers: sessionHeaders(),
        });
        if (refreshRes.ok) {
          const data = await refreshRes.json();
          setRecords(data.records || []);
          setDebugMeta(data.debug_meta || null);
        }
      }
    } catch (e) {
      console.error("Failed to save worker alias:", e);
    }
  };

  const handleDownloadPDF = () => {
    const doc = new jsPDF();
    doc.setFontSize(18);
    doc.setTextColor(30, 41, 59);
    doc.text("Attendance Preview Report", 14, 22);
    doc.setFontSize(10);
    doc.setTextColor(100, 116, 139);
    doc.text(`Generated: ${new Date().toLocaleString()}`, 14, 30);

    const tableColumn = ["Date", "Worker", "Project", "Category", "Description", "Duration", "Status"];
    const tableRows = filteredRecords.map((r) => [
      r.attendance_date, r.worker_name, r.project_name,
      r.boq_category || "—", r.description || "—", r.duration, r.status.toUpperCase(),
    ]);

    autoTable(doc, {
      head: [tableColumn],
      body: tableRows,
      startY: 38,
      styles: { fontSize: 8, cellPadding: 2.5 },
      headStyles: { fillColor: [20, 184, 166], textColor: [255, 255, 255] },
      alternateRowStyles: { fillColor: [248, 250, 252] },
      columnStyles: { 6: { fontStyle: "bold" } },
      didParseCell: (data) => {
        if (data.section === "body" && data.column.index === 6) {
          if (data.cell.raw === "VALID") data.cell.styles.textColor = [13, 148, 136];
          else if (data.cell.raw === "INVALID") data.cell.styles.textColor = [225, 29, 72];
        }
      },
    });
    doc.save(`attendance_preview_${batchId.substring(0, 8)}.pdf`);
  };

  const getStatusBadge = (s: PreviewRecord["status"]) => {
    const map = {
      valid: "bg-teal-500/10 text-teal-600 dark:text-teal-400",
      warning: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
      invalid: "bg-rose-500/10 text-rose-600 dark:text-rose-400",
    };
    return (
      <span className={`px-2.5 py-0.5 rounded-full text-xs font-semibold ${map[s]}`}>
        {s.charAt(0).toUpperCase() + s.slice(1)}
      </span>
    );
  };

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className="space-y-8 relative z-10">

      {/* Header */}
      <div className="flex flex-col sm:flex-row justify-between sm:items-center gap-4">
        <div>
          <h1 className="text-3xl font-extrabold tracking-tight text-slate-800 dark:text-white">
            Preview Records
          </h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Validate your Google Form first, then approve to submit.
          </p>
        </div>

        {/* Action bar — only when idle or validated */}
        {!loadError && !loading && records.length > 0 && flowStatus !== "submitting" && flowStatus !== "completed" && (
          <div className="flex items-center gap-3 flex-wrap">
            {/* Mode selector */}
            <div className="flex items-center bg-slate-100 dark:bg-slate-800 p-1 rounded-lg">
              {([
                { key: "count_only", label: "Count Only" },
                { key: "dry_run", label: "Browser Dry Run" },
                { key: "test", label: "Test (5)" },
                { key: "production", label: "Production" },
              ] as const).map(({ key, label }) => (
                <button
                  key={key}
                  onClick={() => setMode(key as SubmissionMode)}
                  className={`px-3 py-1.5 text-xs font-semibold rounded-md transition-all ${
                    mode === key
                      ? "bg-white dark:bg-slate-700 text-teal-600 dark:text-teal-400 shadow-sm"
                      : "text-slate-500 hover:text-slate-700 dark:hover:text-slate-300"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>

            {/* Validate Form button */}
            <button
              onClick={handleValidateForm}
              disabled={validating || !formUrl}
              className="px-4 py-2.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-sm font-medium rounded-xl flex items-center gap-2 transition-all cursor-pointer shadow-md"
            >
              {validating ? <Loader2 size={16} className="animate-spin" /> : <ShieldCheck size={16} />}
              {validating ? "Validating…" : "Validate Form"}
            </button>

            {/* Submit button — only unlocks after validation passes (or count_only) */}
            <button
              onClick={handleStartSubmission}
              disabled={flowStatus !== "validated" && mode !== "count_only"}
              title={flowStatus !== "validated" && mode !== "count_only" ? "Run 'Validate Form' first" : ""}
              className="px-5 py-2.5 bg-teal-600 hover:bg-teal-500 disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-medium rounded-xl flex items-center gap-2 transition-all active:scale-95 cursor-pointer shadow-md"
            >
              <Zap size={16} />
              {mode === "count_only"
                ? "Check Count (No Browser)"
                : mode === "dry_run"
                ? "Run Browser Dry Run"
                : mode === "test"
                ? "Test Submit (5)"
                : "Approve & Submit"}
            </button>
          </div>
        )}
      </div>

      {/* Body */}
      {loading ? (
        <div className="min-h-[300px] flex items-center justify-center">
          <Loader2 className="animate-spin text-teal-500" size={32} />
        </div>
      ) : loadError ? (
        <div className="min-h-[300px] flex flex-col items-center justify-center gap-4 glass-panel p-10">
          <AlertTriangle size={40} className="text-amber-500" />
          <p className="text-slate-700 dark:text-slate-200 font-semibold text-center max-w-md">{loadError}</p>
          <button
            onClick={() => router.push("/upload")}
            className="mt-2 px-6 py-2.5 bg-teal-600 hover:bg-teal-500 text-white text-sm font-medium rounded-xl cursor-pointer"
          >
            Go to Upload
          </button>
        </div>
      ) : (
        <div className="space-y-6">

          {/* Validation Report Panel */}
          {validationReport && (
            <div className={`glass-panel p-5 border-l-4 animate-in fade-in duration-300 ${
              validationReport.passed ? "border-l-teal-500" : "border-l-rose-500"
            }`}>
              <div className="flex items-start justify-between gap-4">
                <div className="flex items-center gap-3">
                  {validationReport.passed
                    ? <ShieldCheck size={24} className="text-teal-500 shrink-0" />
                    : <ShieldAlert size={24} className="text-rose-500 shrink-0" />
                  }
                  <div>
                    <h3 className={`font-bold text-base ${validationReport.passed ? "text-teal-700 dark:text-teal-300" : "text-rose-700 dark:text-rose-300"}`}>
                      {validationReport.passed ? "Form Validation Passed" : "Form Validation Failed"}
                    </h3>
                    <p className="text-sm text-slate-500 dark:text-slate-400 mt-0.5">{validationReport.message}</p>
                  </div>
                </div>
                <button onClick={() => setValidationReport(null)} className="text-slate-400 hover:text-slate-600 dark:hover:text-white shrink-0">
                  <X size={18} />
                </button>
              </div>

              <div className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-4">
                {/* Found fields */}
                <div>
                  <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500 mb-2">
                    Detected Fields ({validationReport.found.length})
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {validationReport.found.map((f) => (
                      <span key={f} className="flex items-center gap-1 bg-teal-100 dark:bg-teal-900/40 text-teal-700 dark:text-teal-300 text-xs px-2 py-0.5 rounded-full font-medium">
                        <Check size={10} /> {FIELD_LABELS[f] || f}
                      </span>
                    ))}
                    {validationReport.found.length === 0 && <span className="text-slate-400 text-xs">None</span>}
                  </div>
                </div>

                {/* Missing fields */}
                {validationReport.missing.length > 0 && (
                  <div>
                    <div className="text-[10px] font-bold uppercase tracking-wider text-rose-500 mb-2">
                      Missing Fields ({validationReport.missing.length})
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {validationReport.missing.map((f) => (
                        <span key={f} className="flex items-center gap-1 bg-rose-100 dark:bg-rose-900/40 text-rose-700 dark:text-rose-300 text-xs px-2 py-0.5 rounded-full font-medium">
                          <XCircle size={10} /> {FIELD_LABELS[f] || f}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Debug Summary Panel */}
          {debugMeta && (
            <div className="glass-panel p-5 border-l-4 border-l-indigo-500 shadow-sm animate-in fade-in duration-500">
              <h3 className="text-sm font-bold text-slate-800 dark:text-white mb-3 flex items-center gap-2">
                <Search size={16} className="text-indigo-500" />
                Parser Audit
              </h3>
              <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
                <div className="bg-slate-50 dark:bg-slate-800 p-3 rounded-lg border border-slate-200 dark:border-slate-700">
                  <div className="text-[10px] text-slate-500 font-bold uppercase tracking-wider mb-1">Unique Workers</div>
                  <div className="text-xl font-bold text-slate-800 dark:text-white">{debugMeta?.parser?.unique_workers?.length || 0}</div>
                </div>
                <div className="bg-slate-50 dark:bg-slate-800 p-3 rounded-lg border border-slate-200 dark:border-slate-700">
                  <div className="text-[10px] text-slate-500 font-bold uppercase tracking-wider mb-1">Raw Entries</div>
                  <div className="text-xl font-bold text-slate-800 dark:text-white">{debugMeta?.parser?.total_extracted_raw || 0}</div>
                </div>
                <div className="bg-indigo-50 dark:bg-indigo-900/20 p-3 rounded-lg border border-indigo-200 dark:border-indigo-800">
                  <div className="text-[10px] text-indigo-600 dark:text-indigo-400 font-bold uppercase tracking-wider mb-1">Generated</div>
                  <div className="text-xl font-bold text-indigo-700 dark:text-indigo-300">{debugMeta?.rules?.total_after_expansion || 0}</div>
                </div>
                <div className="bg-amber-50 dark:bg-amber-900/20 p-3 rounded-lg border border-amber-200 dark:border-amber-800">
                  <div className="text-[10px] text-amber-600 dark:text-amber-400 font-bold uppercase tracking-wider mb-1">Duplicates</div>
                  <div className="text-xl font-bold text-amber-700 dark:text-amber-300">{debugMeta?.rules?.duplicates_detected || 0}</div>
                </div>
                <div className="col-span-2 bg-slate-50 dark:bg-slate-800 p-3 rounded-lg border border-slate-200 dark:border-slate-700 flex flex-col justify-center">
                  <div className="text-[10px] text-slate-500 font-bold uppercase tracking-wider mb-2">Code Breakdown</div>
                  <div className="flex gap-2 flex-wrap text-xs font-semibold">
                    <span className="bg-teal-100 text-teal-800 dark:bg-teal-900/50 dark:text-teal-300 px-2 py-0.5 rounded">P: {debugMeta?.parser?.attendance_codes?.P || 0}</span>
                    <span className="bg-blue-100 text-blue-800 dark:bg-blue-900/50 dark:text-blue-300 px-2 py-0.5 rounded">P.5: {debugMeta?.parser?.attendance_codes?.["P.5"] || 0}</span>
                    <span className="bg-purple-100 text-purple-800 dark:bg-purple-900/50 dark:text-purple-300 px-2 py-0.5 rounded">PP: {debugMeta?.parser?.attendance_codes?.PP || 0}</span>
                    <span className="bg-slate-200 text-slate-800 dark:bg-slate-700 dark:text-slate-300 px-2 py-0.5 rounded">A: {debugMeta?.parser?.attendance_codes?.A || 0}</span>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Project Resolution Summary */}
          {debugMeta?.project_resolution && debugMeta.project_resolution.length > 0 && (
            <div className="glass-panel p-6 space-y-4 bg-slate-900/40 border-slate-800 shadow-lg transition-all duration-300">
              <div className="flex justify-between items-center cursor-pointer" onClick={() => setResolutionSummaryOpen(!resolutionSummaryOpen)}>
                <h3 className="font-bold text-slate-800 dark:text-white flex items-center gap-2">
                  <GitCompare size={18} className="text-teal-400" />
                  Project Resolution Summary
                </h3>
                <span className="text-xs text-slate-500 hover:text-slate-350 transition-colors">
                  {resolutionSummaryOpen ? "Collapse [-]" : "Expand [+]"}
                </span>
              </div>
              
              {resolutionSummaryOpen && (
                <div className="overflow-x-auto pt-2 animate-in fade-in duration-200">
                  <table className="w-full text-left border-collapse text-xs">
                    <thead>
                      <tr className="border-b border-slate-850 text-slate-400 font-semibold uppercase tracking-wider pb-2">
                        <th className="py-2.5 px-4">Input Project</th>
                        <th className="py-2.5 px-4">Resolved Project</th>
                        <th className="py-2.5 px-4">Confidence</th>
                        <th className="py-2.5 px-4">Match Method</th>
                        <th className="py-2.5 px-4">Status</th>
                        <th className="py-2.5 px-4 text-right">Correct Mappings</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-850">
                      {debugMeta.project_resolution.map((res: any, idx: number) => (
                        <tr key={idx} className="hover:bg-slate-900/20 transition-colors">
                           <td className="py-3 px-4 font-semibold text-slate-300">{res.input_project}</td>
                          <td className="py-3 px-4 text-slate-400 font-medium">
                            {res.resolved_project || <span className="text-amber-400 italic">Unresolved (Select from dropdown)</span>}
                          </td>
                          <td className="py-3 px-4 font-mono font-bold text-teal-400">{res.confidence}%</td>
                          <td className="py-3 px-4 text-slate-500 italic">{res.match_type}</td>
                          <td className="py-3 px-4">
                            <span className={`px-2.5 py-0.5 rounded-full text-[10px] font-bold ${
                              res.status === "Auto-Accepted" ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/10" :
                              res.status === "Smart Match" ? "bg-blue-500/10 text-blue-400 border border-blue-500/10" :
                              "bg-amber-500/10 text-amber-400 border border-amber-500/20 animate-pulse"
                            }`}>
                              {res.status}
                            </span>
                          </td>
                          <td className="py-3 px-4 text-right">
                            <select
                              className={`glass-input text-[11px] py-1.5 px-2.5 bg-[#0f172a] border rounded-xl cursor-pointer max-w-[240px] text-slate-200 focus:outline-none focus:border-teal-500 ${
                                !projectCatalog.includes(res.resolved_project) ? "border-amber-500/50" : "border-slate-800"
                              }`}
                              value={projectCatalog.includes(res.resolved_project) ? res.resolved_project : ""}
                              onChange={(e) => handleAliasMappingCorrection(res.input_project, e.target.value)}
                            >
                              <option value="" className="bg-[#0f172a] text-slate-400">-- Select Project from Form --</option>
                              {projectCatalog.map((p) => (
                                <option key={p} value={p} className="bg-[#0f172a] text-slate-200">{p}</option>
                              ))}
                            </select>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* Worker Resolution Summary */}
          {debugMeta?.worker_resolution && debugMeta.worker_resolution.length > 0 && (
            <div className="glass-panel p-6 space-y-4 bg-slate-900/40 border-slate-800 shadow-lg transition-all duration-300">
              <div className="flex justify-between items-center cursor-pointer" onClick={() => setWorkerResolutionSummaryOpen(!workerResolutionSummaryOpen)}>
                <h3 className="font-bold text-slate-800 dark:text-white flex items-center gap-2">
                  <GitCompare size={18} className="text-teal-400" />
                  Worker Resolution Summary
                </h3>
                <span className="text-xs text-slate-500 hover:text-slate-350 transition-colors">
                  {workerResolutionSummaryOpen ? "Collapse [-]" : "Expand [+]"}
                </span>
              </div>
              
              {workerResolutionSummaryOpen && (
                <div className="overflow-x-auto pt-2 animate-in fade-in duration-200">
                  <table className="w-full text-left border-collapse text-xs">
                    <thead>
                      <tr className="border-b border-slate-850 text-slate-400 font-semibold uppercase tracking-wider pb-2">
                        <th className="py-2.5 px-4">Input Worker</th>
                        <th className="py-2.5 px-4">Resolved Worker</th>
                        <th className="py-2.5 px-4">Confidence</th>
                        <th className="py-2.5 px-4">Match Method</th>
                        <th className="py-2.5 px-4">Status</th>
                        <th className="py-2.5 px-4 text-right">Correct Mappings</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-850">
                      {debugMeta.worker_resolution.map((res: any, idx: number) => (
                        <tr key={idx} className="hover:bg-slate-900/20 transition-colors">
                          <td className="py-3 px-4 font-semibold text-slate-300">{res.input_worker}</td>
                          <td className="py-3 px-4 text-slate-400 font-medium">{res.resolved_worker}</td>
                          <td className="py-3 px-4 font-mono font-bold text-teal-400">{res.confidence}%</td>
                          <td className="py-3 px-4 text-slate-500 italic">{res.match_type}</td>
                          <td className="py-3 px-4">
                            <span className={`px-2.5 py-0.5 rounded-full text-[10px] font-bold ${
                              res.status === "Auto-Accepted" ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/10" :
                              res.status === "Smart Match" ? "bg-blue-500/10 text-blue-400 border border-blue-500/10" :
                              "bg-amber-500/10 text-amber-400 border border-amber-500/20 animate-pulse"
                            }`}>
                              {res.status}
                            </span>
                          </td>
                          <td className="py-3 px-4 text-right">
                            <select
                              className="glass-input text-[11px] py-1.5 px-2.5 bg-slate-950 border border-slate-800 rounded-xl cursor-pointer max-w-[200px] text-slate-200 focus:outline-none focus:border-teal-500 w-full bg-[#0f172a]"
                              value={res.resolved_worker}
                              onChange={(e) => handleWorkerAliasMappingCorrection(res.input_worker, e.target.value)}
                            >
                              <option value="" className="bg-[#0f172a] text-slate-200">-- Correct Mapping --</option>
                              {res.input_worker && !workerCatalog.includes(res.input_worker) && (
                                <option value={res.input_worker} className="bg-[#0f172a] text-slate-200">{res.input_worker}</option>
                              )}
                              {res.resolved_worker && !workerCatalog.includes(res.resolved_worker) && res.resolved_worker !== res.input_worker && res.resolved_worker !== "Needs Review" && (
                                <option value={res.resolved_worker} className="bg-[#0f172a] text-slate-200">{res.resolved_worker}</option>
                              )}
                              {workerCatalog.map((w) => (
                                <option key={w} value={w} className="bg-[#0f172a] text-slate-200">{w}</option>
                              ))}

                            </select>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          <div className="grid grid-cols-1 xl:grid-cols-4 gap-8">
            {/* ── Left: Records Table ─────────────────────────────── */}
            <div className="xl:col-span-3 glass-panel p-6 flex flex-col min-h-[500px]">

              {/* Search + Filter + PDF */}
              <div className="flex flex-col sm:flex-row gap-4 mb-6">
                <div className="relative flex-1">
                  <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
                  <input
                    type="text"
                    placeholder="Search by worker or project..."
                    className="w-full pl-10 pr-4 py-2 glass-input text-sm"
                    value={searchQuery}
                    onChange={(e) => { setSearchQuery(e.target.value); setCurrentPage(1); }}
                  />
                </div>
                <div className="relative w-full sm:w-48">
                  <Filter className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
                  <select
                    className="w-full pl-10 pr-4 py-2 glass-input text-sm appearance-none bg-[#0f172a] text-slate-200 cursor-pointer"
                    value={filterStatus}
                    onChange={(e) => { setFilterStatus(e.target.value); setCurrentPage(1); }}
                  >
                    <option value="all" className="bg-[#0f172a] text-slate-200">All Statuses</option>
                    <option value="valid" className="bg-[#0f172a] text-slate-200">Valid Only</option>
                    <option value="warning" className="bg-[#0f172a] text-slate-200">Warnings</option>
                    <option value="invalid" className="bg-[#0f172a] text-slate-200">Invalid / Errors</option>
                  </select>
                </div>
                <button
                  onClick={handleDownloadPDF}
                  className="w-full sm:w-auto px-4 py-2 bg-slate-100 hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-200 text-sm font-medium rounded-lg flex items-center justify-center gap-2 transition-all cursor-pointer border border-slate-200 dark:border-slate-700"
                >
                  <FileDown size={16} /> Download PDF
                </button>
              </div>

              {/* Table */}
              <div className="overflow-x-auto flex-1">
                <table className="w-full text-left border-collapse">
                  <thead>
                    <tr className="border-b border-slate-200/50 dark:border-slate-800/50 text-slate-400 text-[11px] uppercase tracking-wider">
                      <th className="py-3 px-4 font-semibold">Date</th>
                      <th className="py-3 px-4 font-semibold">Worker</th>
                      <th className="py-3 px-4 font-semibold">Project</th>
                      <th className="py-3 px-4 font-semibold">BOQ Category</th>
                      <th className="py-3 px-4 font-semibold">Description</th>
                      <th className="py-3 px-4 font-semibold">Duration</th>
                      <th className="py-3 px-4 font-semibold">Status</th>
                      <th className="py-3 px-4 font-semibold text-right">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100/50 dark:divide-slate-800/30 text-sm">
                    {paginatedRecords.length > 0 ? (
                      paginatedRecords.map((record) => (
                        <tr key={record.id} className="hover:bg-slate-100/30 dark:hover:bg-slate-800/10 transition-all">
                          <td className="py-3 px-4 font-medium text-slate-700 dark:text-slate-200 whitespace-nowrap">{record.attendance_date}</td>
                          <td className="py-3 px-4 font-semibold text-slate-800 dark:text-white whitespace-nowrap">{record.worker_name}</td>
                          <td className="py-3 px-4 whitespace-nowrap">{record.project_name}</td>
                          <td className="py-3 px-4 text-xs">{record.boq_category || "—"}</td>
                          <td className="py-3 px-4 text-xs">{record.description || "—"}</td>
                          <td className="py-3 px-4 text-xs whitespace-nowrap">{record.duration}</td>
                          <td className="py-3 px-4">{getStatusBadge(record.status)}</td>
                          <td className="py-3 px-4 text-right">
                            <button
                              onClick={() => setEditingRecord(record)}
                              className="p-1.5 text-slate-400 hover:text-teal-500 hover:bg-teal-500/10 rounded-lg transition-colors cursor-pointer"
                            >
                              <Edit2 size={16} />
                            </button>
                          </td>
                        </tr>
                      ))
                    ) : (
                      <tr>
                        <td colSpan={8} className="py-12 text-center text-slate-500 text-sm">
                          {records.length === 0 ? "No records found for this batch." : "No records match your filters."}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>

              {/* Pagination */}
              <div className="mt-4 pt-4 border-t border-slate-200/50 dark:border-slate-800/50 flex items-center justify-between text-sm">
                <span className="text-slate-500">
                  {filteredRecords.length === 0
                    ? "0 records"
                    : `Showing ${(currentPage - 1) * recordsPerPage + 1}–${Math.min(currentPage * recordsPerPage, filteredRecords.length)} of ${filteredRecords.length}`}
                </span>
                <div className="flex gap-2">
                  <button
                    onClick={() => setCurrentPage(Math.max(1, currentPage - 1))}
                    disabled={currentPage === 1}
                    className="p-2 rounded-lg border border-slate-200 dark:border-slate-800 hover:bg-slate-100 dark:hover:bg-slate-800 disabled:opacity-50 cursor-pointer"
                  >
                    <ChevronLeft size={16} />
                  </button>
                  <button
                    onClick={() => setCurrentPage(Math.min(totalPages, currentPage + 1))}
                    disabled={currentPage === totalPages || totalPages === 0}
                    className="p-2 rounded-lg border border-slate-200 dark:border-slate-800 hover:bg-slate-100 dark:hover:bg-slate-800 disabled:opacity-50 cursor-pointer"
                  >
                    <ChevronRight size={16} />
                  </button>
                </div>
              </div>
            </div>

            {/* ── Right: Submission Monitor ──────────────────────── */}
            <div 
              onClick={() => setIsMonitorExpanded(true)}
              className="glass-panel p-6 flex flex-col h-full cursor-pointer hover:border-slate-300 dark:hover:border-slate-700 hover:shadow-lg transition-all group"
            >
              <div className="flex items-center justify-between mb-6">
                <h3 className="font-bold text-slate-800 dark:text-white">Submission Monitor</h3>
                <Maximize2 size={16} className="text-slate-400 group-hover:text-slate-600 dark:group-hover:text-white transition-colors" />
              </div>

              {flowStatus === "idle" && (
                <div className="flex-1 flex flex-col items-center justify-center text-slate-400 dark:text-slate-500 border border-dashed border-slate-300 dark:border-slate-800 rounded-xl bg-slate-50/50 dark:bg-slate-900/30 p-6 text-center gap-3">
                  <ShieldCheck size={32} className="opacity-30" />
                  <span className="text-xs">
                    Click <strong>"Validate Form"</strong> first<br />
                    to check your Google Form, then submit.
                  </span>
                </div>
              )}

              {flowStatus === "validated" && (
                <div className="flex-1 flex flex-col items-center justify-center text-slate-400 dark:text-slate-500 border border-dashed border-teal-300 dark:border-teal-800 rounded-xl bg-teal-50/30 dark:bg-teal-900/10 p-6 text-center gap-3">
                  <CheckCircle size={32} className="text-teal-500" />
                  <span className="text-xs text-teal-700 dark:text-teal-400">
                    Validation passed!<br />
                    Click <strong>"Approve & Submit"</strong> to start.
                  </span>
                </div>
              )}

              {(flowStatus === "submitting" || flowStatus === "completed" || flowStatus === "cancelled" || flowStatus === "failed") && liveStatus && (
                <div className="space-y-5 flex-1 flex flex-col animate-in fade-in zoom-in-95 duration-300">
                  {/* KPIs */}
                  <div className={`grid ${(liveStatus.needs_review || 0) > 0 ? "grid-cols-3" : "grid-cols-2"} gap-2`}>
                    <div className="bg-slate-100 dark:bg-slate-800/50 p-3 rounded-lg border border-slate-200 dark:border-slate-800">
                      <div className="flex items-center gap-1.5 text-teal-600 dark:text-teal-400 mb-1">
                        <Check size={14} />
                        <span className="text-[10px] font-bold uppercase tracking-wider">
                          {liveStatus.dry_run_verified && liveStatus.submitted === 0 ? "Verified" : "Submitted"}
                        </span>
                      </div>
                      <span className="text-2xl font-bold text-slate-800 dark:text-white">
                        {liveStatus.submitted > 0 ? liveStatus.submitted : (liveStatus.dry_run_verified || 0)}
                      </span>
                    </div>
                    <div className="bg-slate-100 dark:bg-slate-800/50 p-3 rounded-lg border border-slate-200 dark:border-slate-800">
                      <div className="flex items-center gap-1.5 text-rose-600 dark:text-rose-400 mb-1">
                        <XCircle size={14} />
                        <span className="text-[10px] font-bold uppercase tracking-wider">Failed</span>
                      </div>
                      <span className="text-2xl font-bold text-slate-800 dark:text-white">{liveStatus.failed}</span>
                    </div>
                    {(liveStatus.needs_review || 0) > 0 && (
                      <div className="bg-amber-50 dark:bg-amber-950/30 p-3 rounded-lg border border-amber-300 dark:border-amber-800/50">
                        <div className="flex items-center gap-1.5 text-amber-600 dark:text-amber-400 mb-1">
                          <AlertTriangle size={14} />
                          <span className="text-[10px] font-bold uppercase tracking-wider">Review</span>
                        </div>
                        <span className="text-2xl font-bold text-amber-700 dark:text-amber-300">{liveStatus.needs_review}</span>
                      </div>
                    )}
                  </div>

                  {/* Progress Bar */}
                  <div className="space-y-2">
                    <div className="flex justify-between text-xs font-semibold">
                      <span className={`uppercase tracking-wider ${
                         flowStatus === "completed" ? "text-teal-500" :
                         flowStatus === "failed" ? "text-rose-500" :
                         flowStatus === "cancelled" ? "text-amber-500" : "text-teal-400"
                      }`}>
                        {liveStatus.batch_status}
                      </span>
                      <span>{liveStatus.progress_pct}%</span>
                    </div>
                    <div className="w-full bg-slate-200 dark:bg-slate-800 h-2 rounded-full overflow-hidden">
                      <div
                        className={`h-full rounded-full transition-all duration-500 ${
                          flowStatus === "cancelled" ? "bg-amber-500" :
                          flowStatus === "failed" ? "bg-rose-500" : "bg-teal-500 shadow-[0_0_10px_rgba(20,184,166,0.8)]"
                        }`}
                        style={{ width: `${liveStatus.progress_pct}%` }}
                      />
                    </div>
                    <div className="text-xs text-slate-500 text-right">
                      {(liveStatus.submitted > 0 ? liveStatus.submitted : (liveStatus.dry_run_verified || 0)) + liveStatus.failed + (liveStatus.needs_review || 0)} / {liveStatus.total} processed
                    </div>
                  </div>

                  {/* Live Logs */}
                  <div className="flex-1 flex flex-col min-h-[160px]">
                    <span className="text-xs font-bold text-slate-400 uppercase tracking-wider block mb-2">Live Output</span>
                    <div 
                      onClick={(e) => e.stopPropagation()}
                      className="bg-slate-950 flex-1 border border-slate-800 rounded-xl p-4 overflow-y-auto font-mono text-[10px] text-teal-400 space-y-1.5 scrollbar-none shadow-inner max-h-[220px]"
                    >
                      {liveStatus.logs.map((log, i) => (
                        <div key={i} className="flex gap-2 leading-relaxed">
                          <span className="text-slate-600">❯</span>
                          <span className={`opacity-90 ${
                            log.includes("[ERROR]") ? "text-rose-400" :
                            log.includes("[NEEDS REVIEW]") ? "text-amber-400 font-semibold" :
                            log.includes("[DRY") || log.includes("[COUNT") ? "text-teal-300" : ""
                          }`}>
                            {log}
                          </span>
                        </div>
                      ))}
                      {liveStatus.logs.length === 0 && (
                        <div className="flex gap-2 text-slate-600">
                          <span>❯</span><span>Waiting for submissions…</span>
                        </div>
                      )}
                    </div>
                  </div>

                  {/* Controls */}
                  <div className="grid grid-cols-2 gap-3">
                    {flowStatus === "submitting" && (
                      <>
                        <button
                          onClick={(e) => { e.stopPropagation(); handlePause(); }}
                          className="py-2.5 bg-amber-500/10 hover:bg-amber-500/20 border border-amber-500/30 text-amber-600 dark:text-amber-400 text-xs font-bold uppercase tracking-wider rounded-xl flex items-center justify-center gap-2 cursor-pointer"
                        >
                          <Pause size={14} /> Pause
                        </button>
                        <button
                          onClick={(e) => { e.stopPropagation(); handleCancel(); }}
                          className="py-2.5 bg-rose-500/10 hover:bg-rose-500/20 border border-rose-500/30 text-rose-600 dark:text-rose-400 text-xs font-bold uppercase tracking-wider rounded-xl flex items-center justify-center gap-2 cursor-pointer"
                        >
                          <Square size={14} /> Cancel
                        </button>
                      </>
                    )}
                    {(flowStatus === "completed" || flowStatus === "cancelled" || flowStatus === "failed") && (
                      <button
                        onClick={(e) => { e.stopPropagation(); router.push("/reports"); }}
                        className="col-span-2 py-2.5 bg-slate-800 hover:bg-slate-700 text-white text-xs font-medium rounded-xl flex items-center justify-center gap-2 cursor-pointer shadow-lg"
                      >
                        View Final Report <ChevronRight size={14} />
                      </button>
                    )}
                  </div>
                </div>
              )}

              {/* Dry run completed without live status */}
              {flowStatus === "completed" && !liveStatus && (
                <div className="flex-1 flex flex-col items-center justify-center gap-4">
                  <CheckCircle size={40} className="text-teal-500" />
                  <p className="text-sm text-slate-500">Dry run completed.</p>
                  <button onClick={(e) => { e.stopPropagation(); router.push("/reports"); }} className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-white text-xs rounded-xl cursor-pointer">
                    View Reports
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Edit Modal */}
      {editingRecord && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/60 backdrop-blur-sm p-4 animate-in fade-in duration-200">
          <div className="glass-panel w-full max-w-md bg-white/90 dark:bg-slate-900/90 shadow-2xl p-6 relative">
            <button onClick={() => setEditingRecord(null)} className="absolute top-4 right-4 text-slate-400 hover:text-slate-600 dark:hover:text-white">
              <X size={18} />
            </button>
            <h2 className="text-xl font-bold text-slate-800 dark:text-white mb-6">Edit Record</h2>
            <form onSubmit={handleSaveEdit} className="space-y-4">
              <div>
                <label className="text-xs font-semibold text-slate-500 block mb-1">Worker Name</label>
                <select
                  value={editingRecord.worker_name}
                  onChange={(e) => setEditingRecord({ ...editingRecord, worker_name: e.target.value })}
                  className="w-full glass-input text-sm bg-[#0f172a] border border-slate-200 dark:border-slate-800 rounded-lg cursor-pointer text-slate-200"
                  required
                >
                  <option value="" className="bg-[#0f172a] text-slate-200">-- Select Worker --</option>
                  {workerCatalog.map((w) => (
                    <option key={w} value={w} className="bg-[#0f172a] text-slate-200">{w}</option>
                  ))}
                  {editingRecord.worker_name && !workerCatalog.includes(editingRecord.worker_name) && (
                    <option value={editingRecord.worker_name} className="bg-[#0f172a] text-slate-200">{editingRecord.worker_name}</option>
                  )}

                </select>
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-500 block mb-1">Project Name</label>
                <select
                  value={editingRecord.project_name}
                  onChange={(e) => setEditingRecord({ ...editingRecord, project_name: e.target.value })}
                  className="w-full glass-input text-sm bg-[#0f172a] border border-slate-200 dark:border-slate-800 rounded-lg cursor-pointer text-slate-200"
                  required
                >
                  <option value="" className="bg-[#0f172a] text-slate-200">-- Select Project --</option>
                  {projectCatalog.map((p) => (
                    <option key={p} value={p} className="bg-[#0f172a] text-slate-200">{p}</option>
                  ))}
                  {editingRecord.project_name && !projectCatalog.includes(editingRecord.project_name) && (
                    <option value={editingRecord.project_name} className="bg-[#0f172a] text-slate-200">{editingRecord.project_name}</option>
                  )}
                </select>
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-500 block mb-1">Description</label>
                <input
                  type="text"
                  value={editingRecord.description || ""}
                  onChange={(e) => setEditingRecord({ ...editingRecord, description: e.target.value })}
                  className="w-full glass-input text-sm"
                />
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-500 block mb-1">Duration</label>
                <select
                  value={
                    durationOptions.find((opt) => opt.toLowerCase() === (editingRecord.duration || "").toLowerCase()) ||
                    editingRecord.duration ||
                    ""
                  }
                  onChange={(e) => setEditingRecord({ ...editingRecord, duration: e.target.value })}
                  className="w-full glass-input text-sm bg-[#0f172a] border border-slate-200 dark:border-slate-800 rounded-lg cursor-pointer text-slate-200"
                  required
                >
                  <option value="" className="bg-[#0f172a] text-slate-200">-- Select Duration --</option>
                  {durationOptions.map((opt) => (
                    <option key={opt} value={opt} className="bg-[#0f172a] text-slate-200">{opt}</option>
                  ))}
                </select>
              </div>
              <div className="pt-4 flex justify-end gap-2">
                <button type="button" onClick={() => setEditingRecord(null)} className="px-4 py-2 text-sm font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800 rounded-lg cursor-pointer">
                  Cancel
                </button>
                <button type="submit" className="px-6 py-2 text-sm font-medium bg-teal-600 hover:bg-teal-500 text-white rounded-lg shadow-lg cursor-pointer">
                  Save Changes
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Expanded Monitor Modal */}
      {isMonitorExpanded && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/60 backdrop-blur-sm p-4 animate-in fade-in duration-200">
          <div className="glass-panel w-full max-w-2xl bg-white/95 dark:bg-slate-900/95 shadow-2xl p-8 relative flex flex-col max-h-[90vh]">
            <button 
              onClick={() => setIsMonitorExpanded(false)} 
              className="absolute top-6 right-6 text-slate-400 hover:text-slate-600 dark:hover:text-white cursor-pointer transition-colors"
            >
              <X size={20} />
            </button>
            
            <h2 className="text-2xl font-bold text-slate-800 dark:text-white mb-6">Submission Monitor (Expanded)</h2>
            
            <div className="flex-1 flex flex-col space-y-6 overflow-y-auto pr-1">
              {flowStatus === "idle" && (
                <div className="flex-1 flex flex-col items-center justify-center text-slate-400 dark:text-slate-500 border border-dashed border-slate-300 dark:border-slate-800 rounded-xl bg-slate-50/50 dark:bg-slate-900/30 p-12 text-center gap-3">
                  <ShieldCheck size={48} className="opacity-30" />
                  <span className="text-sm">
                    Click <strong>"Validate Form"</strong> first<br />
                    to check your Google Form, then submit.
                  </span>
                </div>
              )}

              {flowStatus === "validated" && (
                <div className="flex-1 flex flex-col items-center justify-center text-slate-400 dark:text-slate-500 border border-dashed border-teal-300 dark:border-teal-800 rounded-xl bg-teal-50/30 dark:bg-teal-900/10 p-12 text-center gap-3">
                  <CheckCircle size={48} className="text-teal-500" />
                  <span className="text-sm text-teal-700 dark:text-teal-400">
                    Validation passed!<br />
                    Click <strong>"Approve & Submit"</strong> to start.
                  </span>
                </div>
              )}

              {(flowStatus === "submitting" || flowStatus === "completed" || flowStatus === "cancelled" || flowStatus === "failed") && liveStatus && (
                <div className="space-y-6 flex-1 flex flex-col">
                  {/* KPIs */}
                  <div className="grid grid-cols-2 gap-4">
                    <div className="bg-slate-100 dark:bg-slate-800/50 p-4 rounded-xl border border-slate-200 dark:border-slate-800">
                      <div className="flex items-center gap-1.5 text-teal-600 dark:text-teal-400 mb-1">
                        <Check size={16} />
                        <span className="text-xs font-bold uppercase tracking-wider">Submitted</span>
                      </div>
                      <span className="text-3xl font-extrabold text-slate-800 dark:text-white">{liveStatus.submitted}</span>
                    </div>
                    <div className="bg-slate-100 dark:bg-slate-800/50 p-4 rounded-xl border border-slate-200 dark:border-slate-800">
                      <div className="flex items-center gap-1.5 text-rose-600 dark:text-rose-400 mb-1">
                        <XCircle size={16} />
                        <span className="text-xs font-bold uppercase tracking-wider">Failed</span>
                      </div>
                      <span className="text-3xl font-extrabold text-slate-800 dark:text-white">{liveStatus.failed}</span>
                    </div>
                  </div>

                  {/* Progress Bar */}
                  <div className="space-y-3">
                    <div className="flex justify-between text-sm font-semibold">
                      <span className={`uppercase tracking-wider ${
                        flowStatus === "completed" ? "text-teal-500" :
                        flowStatus === "failed" ? "text-rose-500" :
                        flowStatus === "cancelled" ? "text-amber-500" : "text-teal-400"
                      }`}>
                        {liveStatus.batch_status}
                      </span>
                      <span>{liveStatus.progress_pct}%</span>
                    </div>
                    <div className="w-full bg-slate-200 dark:bg-slate-800 h-3 rounded-full overflow-hidden">
                      <div
                        className={`h-full rounded-full transition-all duration-500 ${
                          flowStatus === "cancelled" ? "bg-amber-500" :
                          flowStatus === "failed" ? "bg-rose-500" : "bg-teal-500 shadow-[0_0_10px_rgba(20,184,166,0.8)]"
                        }`}
                        style={{ width: `${liveStatus.progress_pct}%` }}
                      />
                    </div>
                    <div className="text-xs text-slate-500 text-right">
                      {liveStatus.submitted + liveStatus.failed} / {liveStatus.total} processed
                    </div>
                  </div>

                  {/* Live Logs */}
                  <div className="flex-1 flex flex-col min-h-[300px]">
                    <span className="text-xs font-bold text-slate-400 uppercase tracking-wider block mb-2">Live Output</span>
                    <div className="bg-slate-950 flex-1 border border-slate-800 rounded-xl p-4 overflow-y-auto font-mono text-xs text-teal-400 space-y-2 scrollbar-none shadow-inner max-h-[350px]">
                      {liveStatus.logs.map((log, i) => (
                        <div key={i} className="flex gap-2 leading-relaxed">
                          <span className="text-slate-600">❯</span>
                          <span className={`opacity-90 ${log.includes("[ERROR]") ? "text-rose-400" : log.includes("[DRY") ? "text-slate-300" : ""}`}>
                            {log}
                          </span>
                        </div>
                      ))}
                      {liveStatus.logs.length === 0 && (
                        <div className="flex gap-2 text-slate-600">
                          <span>❯</span><span>Waiting for submissions…</span>
                        </div>
                      )}
                    </div>
                  </div>

                  {/* Controls */}
                  <div className="grid grid-cols-2 gap-4 pt-4 border-t border-slate-200/50 dark:border-slate-800/50">
                    {flowStatus === "submitting" && (
                      <>
                        <button
                          onClick={handlePause}
                          className="py-3 bg-amber-500/10 hover:bg-amber-500/20 border border-amber-500/30 text-amber-600 dark:text-amber-400 text-xs font-bold uppercase tracking-wider rounded-xl flex items-center justify-center gap-2 cursor-pointer"
                        >
                          <Pause size={14} /> Pause
                        </button>
                        <button
                          onClick={handleCancel}
                          className="py-3 bg-rose-500/10 hover:bg-rose-500/20 border border-rose-500/30 text-rose-600 dark:text-rose-400 text-xs font-bold uppercase tracking-wider rounded-xl flex items-center justify-center gap-2 cursor-pointer"
                        >
                          <Square size={14} /> Cancel
                        </button>
                      </>
                    )}
                    {(flowStatus === "completed" || flowStatus === "cancelled" || flowStatus === "failed") && (
                      <button
                        onClick={() => { setIsMonitorExpanded(false); router.push("/reports"); }}
                        className="col-span-2 py-3 bg-slate-800 hover:bg-slate-700 text-white text-xs font-medium rounded-xl flex items-center justify-center gap-2 cursor-pointer shadow-lg"
                      >
                        View Final Report <ChevronRight size={14} />
                      </button>
                    )}
                  </div>
                </div>
              )}

              {/* Dry run completed without live status */}
              {flowStatus === "completed" && !liveStatus && (
                <div className="flex-1 flex flex-col items-center justify-center gap-4 py-8">
                  <CheckCircle size={48} className="text-teal-500" />
                  <p className="text-sm text-slate-500">Dry run completed.</p>
                  <button onClick={() => { setIsMonitorExpanded(false); router.push("/reports"); }} className="px-6 py-2.5 bg-slate-800 hover:bg-slate-700 text-white text-xs rounded-xl cursor-pointer">
                    View Reports
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

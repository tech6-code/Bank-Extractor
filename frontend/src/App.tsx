import { useState, useCallback, useMemo, useEffect, type DragEvent } from "react";
import { ArrowLeftRight, CheckSquare, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";

const ROWS_PER_PAGE = 25;
const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

interface Transaction {
  date: string;
  description: string;
  debit: string;
  credit: string;
  balance: string;
}

interface EditableTransactionRow extends Transaction {
  id: string;
  source: "extracted" | "manual";
  originalIndex: number | null;
  isSwapped: boolean;
}

interface TemplateInfo {
  strategy?: string;
  template_matched?: boolean;
  template_id?: number;
  template_bank_name?: string;
  template_saved?: boolean;
}

interface ValidationMismatch {
  row_index: number;
  date: string;
  description: string;
  expected_balance: string;
  actual_balance: string;
  difference: string;
}

interface ValidationResult {
  total_rows: number;
  validated_rows: number;
  valid_rows: number;
  invalid_rows: number;
  skipped_rows: number;
  accuracy_pct: number;
  direction: string;
  mismatches: ValidationMismatch[];
  row_variances?: Record<string, number>;
}

interface ExtractResult {
  file_id: string;
  filename: string;
  columns: string[];
  transactions: Transaction[];
  total_rows: number;
  template?: TemplateInfo;
  validation?: ValidationResult;
}

function createRowId() {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `row-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function normalizeExtractedRow(txn: Transaction, index: number): EditableTransactionRow {
  return {
    id: createRowId(),
    source: "extracted",
    originalIndex: index,
    isSwapped: false,
    ...txn,
  };
}

function createManualRow(): EditableTransactionRow {
  return {
    id: createRowId(),
    source: "manual",
    originalIndex: null,
    isSwapped: false,
    date: "",
    description: "",
    debit: "",
    credit: "",
    balance: "",
  };
}

function getRenderedTransaction(row: EditableTransactionRow): Transaction {
  if (!row.isSwapped) {
    return {
      date: row.date,
      description: row.description,
      debit: row.debit,
      credit: row.credit,
      balance: row.balance,
    };
  }

  return {
    date: row.date,
    description: row.description,
    debit: row.credit,
    credit: row.debit,
    balance: row.balance,
  };
}

function parseAmount(value: string) {
  return parseFloat(value.replace(/,/g, "")) || 0;
}

function App() {
  const [isDragging, setIsDragging] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isDownloading, setIsDownloading] = useState(false);
  const [pdfPassword, setPdfPassword] = useState("");
  const [result, setResult] = useState<ExtractResult | null>(null);
  const [rows, setRows] = useState<EditableTransactionRow[]>([]);
  const [selectedRowIds, setSelectedRowIds] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);

  const renderedRows = useMemo(() => rows.map(getRenderedTransaction), [rows]);

  const totalPages = rows.length > 0
    ? Math.ceil(rows.length / ROWS_PER_PAGE)
    : 0;

  const paginatedRows = useMemo(() => {
    if (!rows.length) return [];
    const start = (page - 1) * ROWS_PER_PAGE;
    return rows.slice(start, start + ROWS_PER_PAGE);
  }, [rows, page]);

  useEffect(() => {
    if (totalPages === 0 && page !== 1) {
      setPage(1);
      return;
    }
    if (totalPages > 0 && page > totalPages) {
      setPage(totalPages);
    }
  }, [page, totalPages]);

  // Set of row indices that failed balance validation (for highlighting)
  const mismatchRows = useMemo(() => {
    if (!result?.validation?.mismatches) return new Set<number>();
    return new Set(result.validation.mismatches.map((m) => m.row_index));
  }, [result]);

  const stats = useMemo(() => {
    if (!renderedRows.length) return null;
    let totalDebit = 0;
    let totalCredit = 0;
    for (const t of renderedRows) {
      if (t.debit) totalDebit += parseAmount(t.debit);
      if (t.credit) totalCredit += parseAmount(t.credit);
    }
    return {
      totalDebit,
      totalCredit,
      debitCount: renderedRows.filter((t) => t.debit).length,
      creditCount: renderedRows.filter((t) => t.credit).length,
    };
  }, [renderedRows]);

  const swappedCount = useMemo(() => rows.filter((row) => row.isSwapped).length, [rows]);
  const selectedCount = selectedRowIds.size;
  const allVisibleRowsSelected = paginatedRows.length > 0 && paginatedRows.every((row) => selectedRowIds.has(row.id));
  const someVisibleRowsSelected = paginatedRows.some((row) => selectedRowIds.has(row.id));

  const handleUpload = useCallback(async (file: File) => {
    setError(null);
    setResult(null);
    setRows([]);
    setSelectedRowIds(new Set());
    setIsLoading(true);
    setPage(1);

    const formData = new FormData();
    formData.append("file", file);
    if (pdfPassword.trim()) formData.append("password", pdfPassword);

    try {
      const res = await fetch(`${API_BASE}/api/extract`, {
        method: "POST",
        body: formData,
      });

      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.detail || "Upload failed");
      }

      const data: ExtractResult = await res.json();
      setRows(data.transactions.map(normalizeExtractedRow));
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setIsLoading(false);
      setPdfPassword("");
    }
  }, [pdfPassword]);

  const handleDownload = useCallback(async () => {
    if (!result) return;
    setIsDownloading(true);

    try {
      const res = await fetch(`${API_BASE}/api/download/${result.file_id}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          rows: renderedRows,
        }),
      });
      if (!res.ok) throw new Error("Download failed");

      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = result.filename.replace(/\.pdf$/i, ".xlsx");
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Download failed");
    } finally {
      setIsDownloading(false);
    }
  }, [renderedRows, result]);

  const toggleRowSwap = useCallback((rowId: string) => {
    setRows((current) => current.map((row) => (
      row.id === rowId
        ? { ...row, isSwapped: !row.isSwapped }
        : row
    )));
  }, []);

  const toggleRowSelection = useCallback((rowId: string) => {
    setSelectedRowIds((current) => {
      const next = new Set(current);
      if (next.has(rowId)) {
        next.delete(rowId);
      } else {
        next.add(rowId);
      }
      return next;
    });
  }, []);

  const toggleVisibleRowSelection = useCallback(() => {
    setSelectedRowIds((current) => {
      const next = new Set(current);
      if (paginatedRows.every((row) => next.has(row.id))) {
        paginatedRows.forEach((row) => next.delete(row.id));
      } else {
        paginatedRows.forEach((row) => next.add(row.id));
      }
      return next;
    });
  }, [paginatedRows]);

  const clearSelectedRows = useCallback(() => {
    setSelectedRowIds(new Set());
  }, []);

  const swapSelectedRows = useCallback(() => {
    if (selectedRowIds.size === 0) return;
    setRows((current) => current.map((row) => (
      selectedRowIds.has(row.id)
        ? { ...row, isSwapped: !row.isSwapped }
        : row
    )));
    setSelectedRowIds(new Set());
  }, [selectedRowIds]);

  const insertManualRowBelow = useCallback((rowId: string) => {
    setRows((current) => {
      const index = current.findIndex((row) => row.id === rowId);
      if (index === -1) return current;
      const next = [...current];
      next.splice(index + 1, 0, createManualRow());
      return next;
    });
  }, []);

  const updateManualRow = useCallback((
    rowId: string,
    field: keyof Transaction,
    value: string,
  ) => {
    setRows((current) => current.map((row) => (
      row.id === rowId
        ? { ...row, [field]: value }
        : row
    )));
  }, []);

  const removeManualRow = useCallback((rowId: string) => {
    setRows((current) => current.filter((row) => row.id !== rowId));
  }, []);

  const onDragOver = (e: DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  };
  const onDragLeave = () => setIsDragging(false);
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    const file = e.dataTransfer.files[0];
    if (file?.name.toLowerCase().endsWith(".pdf")) {
      handleUpload(file);
    } else {
      setError("Please upload a PDF file.");
    }
  };

  const onFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) handleUpload(file);
    e.target.value = "";
  };

  const fmt = (n: number) =>
    n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  return (
    <div className="dark min-h-screen bg-[#0a0a0f] text-white">
      {/* Gradient background accents */}
      <div className="fixed inset-0 overflow-hidden pointer-events-none">
        <div className="absolute -top-40 -right-40 w-96 h-96 bg-indigo-600/10 rounded-full blur-[120px]" />
        <div className="absolute top-1/2 -left-40 w-96 h-96 bg-purple-600/8 rounded-full blur-[120px]" />
      </div>

      <div className="relative z-10 mx-auto max-w-7xl px-4 sm:px-6 lg:px-8 py-8">
        {/* Header */}
        <header className="mb-8">
          <div className="flex items-center gap-3 mb-2">
            <div className="flex items-center justify-center w-10 h-10 rounded-xl bg-gradient-to-br from-indigo-500 to-purple-600 shadow-lg shadow-indigo-500/20">
              <svg className="w-5 h-5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" />
              </svg>
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight bg-gradient-to-r from-white to-white/60 bg-clip-text text-transparent">
                Bank Statement Extractor
              </h1>
              <p className="text-sm text-white/40">
                Extract transactions from PDF bank statements into structured data
              </p>
            </div>
          </div>
        </header>

        {/* Upload area */}
        <div className="mb-6">
          <div className="mb-3">
            <label className="mb-2 block text-xs font-medium uppercase tracking-[0.18em] text-white/40">
              PDF Password
            </label>
            <input
              type="password"
              value={pdfPassword}
              onChange={(e) => setPdfPassword(e.target.value)}
              placeholder="Optional. Required only for protected PDFs"
              className="w-full rounded-xl border border-white/[0.08] bg-white/[0.03] px-4 py-3 text-sm text-white outline-none transition focus:border-indigo-400/60 focus:bg-white/[0.05]"
              disabled={isLoading}
            />
            <p className="mt-2 text-xs text-white/30">
              If the uploaded bank statement PDF is password protected, enter the PDF password here before uploading.
            </p>
          </div>
          <label
            htmlFor="file-upload"
            className={`group relative flex flex-col items-center justify-center rounded-2xl border cursor-pointer transition-all duration-300 overflow-hidden ${
              isDragging
                ? "border-indigo-400/60 bg-indigo-500/5 shadow-lg shadow-indigo-500/10"
                : "border-white/[0.06] bg-white/[0.02] hover:border-white/[0.12] hover:bg-white/[0.04]"
            } ${isLoading ? "pointer-events-none" : ""}`}
            onDragOver={onDragOver}
            onDragLeave={onDragLeave}
            onDrop={onDrop}
          >
            <div className="py-12 px-6 text-center">
              {isLoading ? (
                <div className="flex flex-col items-center gap-4">
                  <div className="relative">
                    <div className="w-12 h-12 rounded-full border-2 border-indigo-500/20" />
                    <div className="absolute inset-0 w-12 h-12 rounded-full border-2 border-transparent border-t-indigo-400 animate-spin" />
                  </div>
                  <div>
                    <p className="text-sm font-medium text-white/70">Processing your statement...</p>
                    <p className="text-xs text-white/30 mt-1">This may take a moment for large files</p>
                  </div>
                </div>
              ) : (
                <>
                  <div className={`mx-auto mb-4 flex items-center justify-center w-14 h-14 rounded-2xl transition-all duration-300 ${
                    isDragging ? "bg-indigo-500/20 scale-110" : "bg-white/[0.04] group-hover:bg-white/[0.06]"
                  }`}>
                    <svg className={`w-6 h-6 transition-colors ${isDragging ? "text-indigo-400" : "text-white/30 group-hover:text-white/50"}`}
                      fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5m-13.5-9L12 3m0 0 4.5 4.5M12 3v13.5" />
                    </svg>
                  </div>
                  <p className="text-sm font-medium text-white/60">
                    Drop your PDF here or{" "}
                    <span className="text-indigo-400 hover:text-indigo-300">browse</span>
                  </p>
                  <p className="text-xs text-white/25 mt-1.5">
                    Supports all major bank statement formats
                  </p>
                </>
              )}
            </div>
            <input
              id="file-upload"
              type="file"
              accept=".pdf"
              className="hidden"
              onChange={onFileSelect}
              disabled={isLoading}
            />
          </label>
        </div>

        {/* Error */}
        {error && (
          <div className="mb-6 flex items-start gap-3 rounded-xl border border-red-500/20 bg-red-500/5 px-4 py-3">
            <svg className="w-5 h-5 text-red-400 shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 3.75h.008v.008H12v-.008Z" />
            </svg>
            <div>
              <p className="text-sm font-medium text-red-300">Extraction failed</p>
              <p className="text-xs text-red-300/60 mt-0.5">{error}</p>
            </div>
          </div>
        )}

        {/* Results */}
        {result && stats && (
          <div className="space-y-4 animate-in fade-in slide-in-from-bottom-4 duration-500">
            {/* Template status badge */}
            {result.template && (
              <div className="flex items-center gap-2">
                {result.template.template_matched ? (
                  <div className="flex items-center gap-2 rounded-lg border border-emerald-500/20 bg-emerald-500/5 px-3 py-1.5">
                    <svg className="w-4 h-4 text-emerald-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" />
                    </svg>
                    <span className="text-xs text-emerald-300 font-medium">
                      Template matched{result.template.template_bank_name ? `: ${result.template.template_bank_name}` : ""}
                    </span>
                    <Badge variant="secondary" className="bg-emerald-500/10 text-emerald-400 border-0 text-[10px] uppercase">
                      {result.template.strategy}
                    </Badge>
                  </div>
                ) : result.template.template_saved ? (
                  <div className="flex items-center gap-2 rounded-lg border border-blue-500/20 bg-blue-500/5 px-3 py-1.5">
                    <svg className="w-4 h-4 text-blue-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v6m3-3H9m12 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" />
                    </svg>
                    <span className="text-xs text-blue-300 font-medium">
                      New template saved{result.template.template_bank_name ? `: ${result.template.template_bank_name}` : ""}
                    </span>
                    <Badge variant="secondary" className="bg-blue-500/10 text-blue-400 border-0 text-[10px] uppercase">
                      {result.template.strategy}
                    </Badge>
                  </div>
                ) : (
                  <div className="flex items-center gap-2 rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-1.5">
                    <span className="text-xs text-white/40 font-medium">
                      Strategy: {result.template.strategy}
                    </span>
                  </div>
                )}
              </div>
            )}

            <div className="rounded-xl border border-white/[0.06] bg-white/[0.02] px-4 py-3">
              <p className="text-sm font-medium text-white/70">Review Controls</p>
              <p className="text-xs text-white/35">
                Select multiple rows to bulk swap them, or use the add icon to insert a manual row below any transaction. Export follows the table exactly as shown.
              </p>
            </div>

            <div className="rounded-xl border border-white/[0.06] bg-white/[0.02] px-4 py-3">
              <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                <div>
                  <p className="text-sm font-medium text-white/70">Bulk Swap</p>
                  <p className="text-xs text-white/35">
                    {selectedCount > 0
                      ? `${selectedCount} transaction row${selectedCount > 1 ? "s" : ""} selected`
                      : "Select transaction rows from the table to swap debit and credit in one action."}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={clearSelectedRows}
                    disabled={selectedCount === 0}
                    className="text-white/50 hover:text-white/80 hover:bg-white/[0.04]"
                  >
                    Clear Selection
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    onClick={swapSelectedRows}
                    disabled={selectedCount === 0}
                    className="bg-indigo-500/20 text-indigo-200 hover:bg-indigo-500/30 border border-indigo-400/20"
                  >
                    <span className="flex items-center gap-2">
                      <CheckSquare className="h-4 w-4" />
                      Swap Selected
                    </span>
                  </Button>
                </div>
              </div>
            </div>

            {swappedCount > 0 && result.validation && result.validation.validated_rows > 0 && (
              <div className="rounded-xl border border-amber-500/20 bg-amber-500/5 px-4 py-3">
                <p className="text-sm font-medium text-amber-200">Some transaction rows currently have debit and credit swapped for review.</p>
                <p className="mt-1 text-xs text-amber-200/70">
                  Balance validation still reflects the original extraction values.
                </p>
              </div>
            )}

            {/* Stats bar */}
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
              <StatCard
                label="Transactions"
                value={rows.length.toLocaleString()}
                icon={
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 12h16.5m-16.5 3.75h16.5M3.75 19.5h16.5M5.625 4.5h12.75a1.875 1.875 0 0 1 0 3.75H5.625a1.875 1.875 0 0 1 0-3.75Z" />
                  </svg>
                }
              />
              <StatCard
                label="Total Debit"
                value={fmt(stats.totalDebit)}
                sub={`${stats.debitCount} entries`}
                color="red"
                icon={
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 6 9 12.75l4.286-4.286a11.948 11.948 0 0 1 4.306 6.43l.776 2.898m0 0 3.182-5.511m-3.182 5.51-5.511-3.181" />
                  </svg>
                }
              />
              <StatCard
                label="Total Credit"
                value={fmt(stats.totalCredit)}
                sub={`${stats.creditCount} entries`}
                color="green"
                icon={
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 18 9 11.25l4.306 4.306a11.95 11.95 0 0 1 5.814-5.518l2.74-1.22m0 0-5.94-2.281m5.94 2.28-2.28 5.941" />
                  </svg>
                }
              />
              {/* Balance Validation Card */}
              {result.validation && result.validation.validated_rows > 0 ? (
                <StatCard
                  label="Balance Check"
                  value={`${result.validation.accuracy_pct}%`}
                  sub={
                    result.validation.invalid_rows > 0
                      ? `${result.validation.invalid_rows} mismatch${result.validation.invalid_rows > 1 ? "es" : ""}`
                      : `${result.validation.valid_rows} verified`
                  }
                  color={
                    result.validation.accuracy_pct >= 95
                      ? "green"
                      : result.validation.accuracy_pct >= 80
                        ? "yellow"
                        : "red"
                  }
                  icon={
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75 11.25 15 15 9.75m-3-7.036A11.959 11.959 0 0 1 3.598 6 11.99 11.99 0 0 0 3 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285Z" />
                    </svg>
                  }
                />
              ) : (
                <StatCard
                  label="Balance Check"
                  value="N/A"
                  sub="No balance data"
                  icon={
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75 11.25 15 15 9.75m-3-7.036A11.959 11.959 0 0 1 3.598 6 11.99 11.99 0 0 0 3 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285Z" />
                    </svg>
                  }
                />
              )}
              <div className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-4 flex flex-col justify-between">
                <div className="flex items-center gap-2 mb-3">
                  <div className="flex items-center justify-center w-7 h-7 rounded-lg bg-indigo-500/10">
                    <svg className="w-4 h-4 text-indigo-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3" />
                    </svg>
                  </div>
                  <span className="text-xs text-white/40 font-medium">{result.filename}</span>
                </div>
                <Button
                  onClick={handleDownload}
                  disabled={isDownloading}
                  className="w-full bg-gradient-to-r from-indigo-500 to-purple-600 hover:from-indigo-400 hover:to-purple-500 text-white border-0 shadow-lg shadow-indigo-500/20 transition-all hover:shadow-indigo-500/30"
                >
                  {isDownloading ? (
                    <span className="flex items-center gap-2">
                      <div className="w-4 h-4 rounded-full border-2 border-transparent border-t-white animate-spin" />
                      Exporting...
                    </span>
                  ) : (
                    <span className="flex items-center gap-2">
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m.75 12 3 3m0 0 3-3m-3 3v-6m-1.5-9H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" />
                      </svg>
                      Download Excel
                    </span>
                  )}
                </Button>
              </div>
            </div>

            {/* Transaction table */}
            <div className="rounded-xl border border-white/[0.06] bg-white/[0.015] overflow-hidden">
              <div className="overflow-auto">
                <Table>
                  <TableHeader>
                    <TableRow className="border-white/[0.06] hover:bg-transparent">
                      <TableHead className="w-12 text-center">
                        <input
                          type="checkbox"
                          checked={allVisibleRowsSelected}
                          ref={(input) => {
                            if (input) input.indeterminate = !allVisibleRowsSelected && someVisibleRowsSelected;
                          }}
                          onChange={toggleVisibleRowSelection}
                          className="h-4 w-4 rounded border-white/20 bg-transparent accent-indigo-500"
                          title="Select all rows on this page"
                        />
                      </TableHead>
                      <TableHead className="text-white/40 font-semibold text-xs uppercase tracking-wider pl-5">Date</TableHead>
                      <TableHead className="text-white/40 font-semibold text-xs uppercase tracking-wider">Description</TableHead>
                      <TableHead className="text-white/40 font-semibold text-xs uppercase tracking-wider text-right">Debit</TableHead>
                      <TableHead className="text-white/40 font-semibold text-xs uppercase tracking-wider text-right">Credit</TableHead>
                      <TableHead className="text-white/40 font-semibold text-xs uppercase tracking-wider text-right">Balance</TableHead>
                      <TableHead className="text-white/40 font-semibold text-xs uppercase tracking-wider text-center">Add</TableHead>
                      <TableHead className="text-white/40 font-semibold text-xs uppercase tracking-wider text-center">Swap</TableHead>
                      {result.validation?.row_variances && (
                        <TableHead className="text-white/40 font-semibold text-xs uppercase tracking-wider text-right pr-5">Variance</TableHead>
                      )}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {paginatedRows.map((row) => {
                      const renderedTxn = getRenderedTransaction(row);
                      const isManualRow = row.source === "manual";
                      const isMismatch = row.originalIndex !== null && mismatchRows.has(row.originalIndex);
                      const variance = row.originalIndex !== null
                        ? result.validation?.row_variances?.[row.originalIndex]
                        : undefined;
                      const hasVariance = variance !== undefined;
                      const isZero = hasVariance && variance === 0;
                      return (
                        <TableRow
                          key={row.id}
                          className={`border-white/[0.03] hover:bg-white/[0.02] transition-colors ${
                            isManualRow
                              ? "bg-indigo-500/[0.04]"
                              : isMismatch
                                ? "border-l-2 border-l-amber-500/60 bg-amber-500/[0.03]"
                                : ""
                          }`}
                          title={
                            isManualRow
                              ? "Manual transaction row"
                              : isMismatch
                                ? "Balance mismatch detected"
                                : undefined
                          }
                        >
                          <TableCell className="text-center align-top pt-4">
                            <input
                              type="checkbox"
                              checked={selectedRowIds.has(row.id)}
                              onChange={() => toggleRowSelection(row.id)}
                              className="h-4 w-4 rounded border-white/20 bg-transparent accent-indigo-500"
                              title="Select this row"
                            />
                          </TableCell>
                          <TableCell className="whitespace-nowrap text-white/50 text-sm font-mono pl-5">
                            {!isManualRow && isMismatch && (
                              <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-400 mr-2 shrink-0" />
                            )}
                            {isManualRow ? (
                              <input
                                type="text"
                                value={row.date}
                                onChange={(e) => updateManualRow(row.id, "date", e.target.value)}
                                placeholder="DD/MM/YYYY"
                                className="w-full min-w-[120px] rounded-lg border border-white/[0.08] bg-white/[0.03] px-3 py-2 text-sm text-white outline-none transition focus:border-indigo-400/60 focus:bg-white/[0.05]"
                              />
                            ) : (
                              renderedTxn.date
                            )}
                          </TableCell>
                          <TableCell className="max-w-xl text-sm text-white/70 align-top">
                            {isManualRow ? (
                              <input
                                type="text"
                                value={row.description}
                                onChange={(e) => updateManualRow(row.id, "description", e.target.value)}
                                placeholder="Enter transaction description"
                                className="w-full rounded-lg border border-white/[0.08] bg-white/[0.03] px-3 py-2 text-sm text-white outline-none transition focus:border-indigo-400/60 focus:bg-white/[0.05]"
                              />
                            ) : (
                              <span className="block whitespace-pre-wrap break-words leading-6">
                                {renderedTxn.description}
                              </span>
                            )}
                          </TableCell>
                          <TableCell className="text-right whitespace-nowrap text-sm font-mono">
                            {isManualRow ? (
                              <input
                                type="text"
                                value={row.isSwapped ? row.credit : row.debit}
                                onChange={(e) => updateManualRow(row.id, row.isSwapped ? "credit" : "debit", e.target.value)}
                                placeholder="0.00"
                                className="w-full min-w-[120px] rounded-lg border border-white/[0.08] bg-white/[0.03] px-3 py-2 text-right text-sm text-white outline-none transition focus:border-indigo-400/60 focus:bg-white/[0.05]"
                              />
                            ) : (
                              renderedTxn.debit && (
                                <span className="text-red-400">{renderedTxn.debit}</span>
                              )
                            )}
                          </TableCell>
                          <TableCell className="text-right whitespace-nowrap text-sm font-mono">
                            {isManualRow ? (
                              <input
                                type="text"
                                value={row.isSwapped ? row.debit : row.credit}
                                onChange={(e) => updateManualRow(row.id, row.isSwapped ? "debit" : "credit", e.target.value)}
                                placeholder="0.00"
                                className="w-full min-w-[120px] rounded-lg border border-white/[0.08] bg-white/[0.03] px-3 py-2 text-right text-sm text-white outline-none transition focus:border-indigo-400/60 focus:bg-white/[0.05]"
                              />
                            ) : (
                              renderedTxn.credit && (
                                <span className="text-emerald-400">{renderedTxn.credit}</span>
                              )
                            )}
                          </TableCell>
                          <TableCell className={`text-right whitespace-nowrap text-sm font-mono ${isMismatch ? "text-amber-400" : "text-white/60"}`}>
                            {isManualRow ? (
                              <input
                                type="text"
                                value={row.balance}
                                onChange={(e) => updateManualRow(row.id, "balance", e.target.value)}
                                placeholder="0.00"
                                className="w-full min-w-[120px] rounded-lg border border-white/[0.08] bg-white/[0.03] px-3 py-2 text-right text-sm text-white outline-none transition focus:border-indigo-400/60 focus:bg-white/[0.05]"
                              />
                            ) : (
                              renderedTxn.balance
                            )}
                          </TableCell>
                          <TableCell className="text-center">
                            <div className="flex items-center justify-center gap-1">
                              <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                onClick={() => insertManualRowBelow(row.id)}
                                className="h-8 w-8 p-0 text-white/40 hover:text-white/80 hover:bg-white/[0.04]"
                                title="Add a manual transaction row below"
                              >
                                <Plus className="h-4 w-4" />
                              </Button>
                              {isManualRow && (
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="sm"
                                  onClick={() => removeManualRow(row.id)}
                                  className="h-8 w-8 p-0 text-red-300/70 hover:text-red-200 hover:bg-red-500/[0.08]"
                                  title="Remove this manual transaction row"
                                >
                                  <Trash2 className="h-4 w-4" />
                                </Button>
                              )}
                            </div>
                          </TableCell>
                          <TableCell className="text-center">
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              onClick={() => toggleRowSwap(row.id)}
                              className={`h-8 w-8 p-0 ${
                                row.isSwapped
                                  ? "text-indigo-300 bg-indigo-500/15 hover:bg-indigo-500/20"
                                  : "text-white/40 hover:text-white/70 hover:bg-white/[0.04]"
                              }`}
                              title={row.isSwapped ? "Show original values" : "Swap debit and credit for this row"}
                            >
                              <ArrowLeftRight className="h-4 w-4" />
                            </Button>
                          </TableCell>
                          {result.validation?.row_variances && (
                            <TableCell className={`text-right whitespace-nowrap text-sm font-mono pr-5 ${
                              !hasVariance ? "text-white/20" :
                              isZero ? "text-emerald-400/70" :
                              "text-red-400"
                            }`}>
                              {!hasVariance ? "-" :
                               isZero ? "0" :
                               (variance > 0 ? "+" : "") + variance.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                            </TableCell>
                          )}
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>

              {/* Pagination */}
              {totalPages > 1 && (
                <div className="flex items-center justify-between border-t border-white/[0.06] px-5 py-3">
                  <p className="text-xs text-white/30">
                    {(page - 1) * ROWS_PER_PAGE + 1}
                    {" - "}
                    {Math.min(page * ROWS_PER_PAGE, rows.length)} of{" "}
                    {rows.length.toLocaleString()}
                  </p>
                  <div className="flex items-center gap-1">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setPage(1)}
                      disabled={page === 1}
                      className="text-white/40 hover:text-white/70 hover:bg-white/[0.04] h-8 w-8 p-0"
                    >
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="m18.75 4.5-7.5 7.5 7.5 7.5m-6-15L5.25 12l7.5 7.5" />
                      </svg>
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setPage((p) => Math.max(1, p - 1))}
                      disabled={page === 1}
                      className="text-white/40 hover:text-white/70 hover:bg-white/[0.04] h-8 w-8 p-0"
                    >
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5 8.25 12l7.5-7.5" />
                      </svg>
                    </Button>
                    <div className="flex items-center gap-1 mx-2">
                      <Badge variant="secondary" className="bg-white/[0.06] text-white/60 border-0 text-xs font-mono">
                        {page}
                      </Badge>
                      <span className="text-white/20 text-xs">/</span>
                      <span className="text-white/30 text-xs font-mono">{totalPages}</span>
                    </div>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                      disabled={page === totalPages}
                      className="text-white/40 hover:text-white/70 hover:bg-white/[0.04] h-8 w-8 p-0"
                    >
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" />
                      </svg>
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setPage(totalPages)}
                      disabled={page === totalPages}
                      className="text-white/40 hover:text-white/70 hover:bg-white/[0.04] h-8 w-8 p-0"
                    >
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="m5.25 4.5 7.5 7.5-7.5 7.5m6-15 7.5 7.5-7.5 7.5" />
                      </svg>
                    </Button>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function StatCard({
  label,
  value,
  sub,
  color,
  icon,
}: {
  label: string;
  value: string;
  sub?: string;
  color?: "red" | "green" | "yellow";
  icon: React.ReactNode;
}) {
  const colorMap = {
    red: {
      iconBg: "bg-red-500/10",
      iconText: "text-red-400",
      valueText: "text-red-400",
    },
    green: {
      iconBg: "bg-emerald-500/10",
      iconText: "text-emerald-400",
      valueText: "text-emerald-400",
    },
    yellow: {
      iconBg: "bg-amber-500/10",
      iconText: "text-amber-400",
      valueText: "text-amber-400",
    },
  };

  const c = color ? colorMap[color] : {
    iconBg: "bg-white/[0.04]",
    iconText: "text-white/40",
    valueText: "text-white/90",
  };

  return (
    <div className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-4">
      <div className="flex items-center gap-2 mb-2">
        <div className={`flex items-center justify-center w-7 h-7 rounded-lg ${c.iconBg}`}>
          <span className={c.iconText}>{icon}</span>
        </div>
        <span className="text-xs text-white/40 font-medium">{label}</span>
      </div>
      <p className={`text-lg font-semibold font-mono tracking-tight ${c.valueText}`}>
        {value}
      </p>
      {sub && <p className="text-xs text-white/25 mt-0.5">{sub}</p>}
    </div>
  );
}

export default App;

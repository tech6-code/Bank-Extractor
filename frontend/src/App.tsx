import { useState, useCallback, useMemo, type DragEvent } from "react";
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

interface TemplateInfo {
  strategy?: string;
  template_matched?: boolean;
  template_id?: number;
  template_bank_name?: string;
  template_saved?: boolean;
}

interface ExtractResult {
  file_id: string;
  filename: string;
  columns: string[];
  transactions: Transaction[];
  total_rows: number;
  template?: TemplateInfo;
}

function App() {
  const [isDragging, setIsDragging] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isDownloading, setIsDownloading] = useState(false);
  const [result, setResult] = useState<ExtractResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);

  const totalPages = result
    ? Math.ceil(result.transactions.length / ROWS_PER_PAGE)
    : 0;

  const paginatedRows = useMemo(() => {
    if (!result) return [];
    const start = (page - 1) * ROWS_PER_PAGE;
    return result.transactions.slice(start, start + ROWS_PER_PAGE);
  }, [result, page]);

  const stats = useMemo(() => {
    if (!result) return null;
    let totalDebit = 0;
    let totalCredit = 0;
    for (const t of result.transactions) {
      if (t.debit) totalDebit += parseFloat(t.debit.replace(/,/g, "")) || 0;
      if (t.credit) totalCredit += parseFloat(t.credit.replace(/,/g, "")) || 0;
    }
    return {
      totalDebit,
      totalCredit,
      debitCount: result.transactions.filter((t) => t.debit).length,
      creditCount: result.transactions.filter((t) => t.credit).length,
    };
  }, [result]);

  const handleUpload = useCallback(async (file: File) => {
    setError(null);
    setResult(null);
    setIsLoading(true);
    setPage(1);

    const formData = new FormData();
    formData.append("file", file);

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
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setIsLoading(false);
    }
  }, []);

  const handleDownload = useCallback(async () => {
    if (!result) return;
    setIsDownloading(true);

    try {
      const res = await fetch(`${API_BASE}/api/download/${result.file_id}`, {
        method: "POST",
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
  }, [result]);

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
                      New template saved
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

            {/* Stats bar */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <StatCard
                label="Transactions"
                value={result.total_rows.toLocaleString()}
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
                      <TableHead className="text-white/40 font-semibold text-xs uppercase tracking-wider pl-5">Date</TableHead>
                      <TableHead className="text-white/40 font-semibold text-xs uppercase tracking-wider">Description</TableHead>
                      <TableHead className="text-white/40 font-semibold text-xs uppercase tracking-wider text-right">Debit</TableHead>
                      <TableHead className="text-white/40 font-semibold text-xs uppercase tracking-wider text-right">Credit</TableHead>
                      <TableHead className="text-white/40 font-semibold text-xs uppercase tracking-wider text-right pr-5">Balance</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {paginatedRows.map((txn, i) => (
                      <TableRow
                        key={`${txn.date}-${txn.balance}-${i}`}
                        className="border-white/[0.03] hover:bg-white/[0.02] transition-colors"
                      >
                        <TableCell className="whitespace-nowrap text-white/50 text-sm font-mono pl-5">
                          {txn.date}
                        </TableCell>
                        <TableCell className="max-w-md text-sm text-white/70">
                          <span className="truncate block">{txn.description}</span>
                        </TableCell>
                        <TableCell className="text-right whitespace-nowrap text-sm font-mono">
                          {txn.debit && (
                            <span className="text-red-400">{txn.debit}</span>
                          )}
                        </TableCell>
                        <TableCell className="text-right whitespace-nowrap text-sm font-mono">
                          {txn.credit && (
                            <span className="text-emerald-400">{txn.credit}</span>
                          )}
                        </TableCell>
                        <TableCell className="text-right whitespace-nowrap text-sm font-mono text-white/60 pr-5">
                          {txn.balance}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>

              {/* Pagination */}
              {totalPages > 1 && (
                <div className="flex items-center justify-between border-t border-white/[0.06] px-5 py-3">
                  <p className="text-xs text-white/30">
                    {(page - 1) * ROWS_PER_PAGE + 1}
                    {" - "}
                    {Math.min(page * ROWS_PER_PAGE, result.total_rows)} of{" "}
                    {result.total_rows.toLocaleString()}
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
  color?: "red" | "green";
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

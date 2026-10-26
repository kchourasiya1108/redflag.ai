import { useEffect, useRef, useState, type KeyboardEvent as KeyEvent } from "react";
import { Search as SearchIcon } from "lucide-react";
import { api } from "../api";
import type { IpoRow } from "../types";
import { date, navigate, rupees, useDebounced } from "../lib";
import { titleCase } from "../pages/Home";

/** Type-ahead over the archive's IPOs: company, symbol, id or lead manager. */
export function IpoSearch({ autoFocus }: { autoFocus?: boolean }) {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<IpoRow[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [open, setOpen] = useState(false);
  const [sel, setSel] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const dq = useDebounced(q, 120);

  useEffect(() => {
    let live = true;
    api.ipos(dq, 30).then((r) => {
      if (!live) return;
      setItems(r.items);
      setTotal(r.total);
      setSel(0);
    }, () => live && setItems([]));
    return () => { live = false; };
  }, [dq]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "/" && document.activeElement?.tagName !== "INPUT") {
        e.preventDefault();
        input.current?.focus();
      }
    };
    const onClick = (e: MouseEvent) => box.current && !box.current.contains(e.target as Node) && setOpen(false);
    window.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClick);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onClick);
    };
  }, []);

  const go = (row: IpoRow) => navigate(`/ipo/${encodeURIComponent(row.ipo_id)}`);
  const onKeyDown = (e: KeyEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); setSel((s) => Math.min(s + 1, items.length - 1)); }
    if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)); }
    if (e.key === "Enter" && items[sel]) go(items[sel]);
    if (e.key === "Escape") { setOpen(false); input.current?.blur(); }
  };

  return (
    <div className="search" ref={box}>
      <SearchIcon className="icon-left" size={19} aria-hidden />
      <input
        ref={input}
        className="search-input"
        placeholder={total ? `Search ${total} SME IPOs by company, symbol or lead manager` : "Search SME IPOs by company, symbol or lead manager"}
        value={q}
        autoFocus={autoFocus}
        onChange={(e) => { setQ(e.target.value); setOpen(true); }}
        onMouseDown={() => setOpen(true)}
        onKeyDown={onKeyDown}
        role="combobox"
        aria-expanded={open}
        aria-controls="ipo-results"
        aria-label="Search IPOs"
      />
      <span className="kbd">/</span>
      {open && (
        <div className="results" id="ipo-results" role="listbox">
          {items.length === 0 && <div className="empty small">No IPO matches “{q}”.</div>}
          {items.map((r, i) => (
            <a key={r.ipo_id} href={`/ipo/${r.ipo_id}`} className="result" role="option" aria-selected={i === sel}
              onMouseEnter={() => setSel(i)} onClick={(e) => { e.preventDefault(); go(r); }}>
              <span className="name">{titleCase(r.company)}</span>
              <span className="side">
                {r.recorded && <span className="pill">Example</span>}
                {!r.has_rhp && <span className="pill" title="Prospectus text is not on file: the check uses the people involved and track records only">No prospectus</span>}
                <span className="pill">{r.exchange.replace(" Emerge", "").replace("BSE SME", "BSE")}</span>
              </span>
              <span className="sub">
                <span className="mono">{r.symbol}</span> · bid opened {date(r.open_date)} · {rupees(r.issue_price)} a share
                {r.banker_raw ? ` · ${r.banker_raw}` : ""}
              </span>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

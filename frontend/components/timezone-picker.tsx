"use client";

import { useMemo, useRef, useState } from "react";
import { ChevronDown, Search } from "lucide-react";
import { ApiError } from "../lib/api";
import { timezoneOptions, timezoneSearch, utcOffset, offsetLabel } from "../lib/timezone";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
} from "./ui/dropdown-menu";

export function TimezonePicker({
  value,
  onChange,
  onUnauthorized,
}: {
  value: string;
  onChange: (timeZone: string) => Promise<void>;
  onUnauthorized: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [referenceDate, setReferenceDate] = useState(() => new Date());
  const options = useRef<HTMLDivElement>(null);
  const timeZones = useMemo(() => timezoneOptions(value, referenceDate), [value, referenceDate]);
  const currentOffset = utcOffset(value, referenceDate);
  const selectedOffset = currentOffset === null ? value : offsetLabel(currentOffset);
  const query = timezoneSearch(search);
  const filtered = timeZones.filter((zone) => zone.searchText.includes(query));

  async function select(timeZone: string) {
    if (busy) return;
    // Selecting the current value also saves an explicit preference over Cal.com's timezone.
    setBusy(true);
    setError("");
    try {
      await onChange(timeZone);
      setOpen(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onUnauthorized();
      else setError(e instanceof Error ? e.message : "Unable to save timezone. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <DropdownMenu
      open={open}
      onOpenChange={(next) => {
        if (busy) return;
        setOpen(next);
        if (next) {
          setReferenceDate(new Date());
          setSearch("");
          setError("");
        }
      }}
    >
      <DropdownMenuTrigger className="timezone" aria-label={`Change timezone, currently ${value}`}>
        <span>{selectedOffset}</span>
        <ChevronDown size={14} aria-hidden="true" />
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        collisionPadding={12}
        className="timezone-menu"
        aria-label="Choose timezone"
      >
        <DropdownMenuLabel>Timezone</DropdownMenuLabel>
        <div className="timezone-search">
          <Search size={15} aria-hidden="true" />
          <input
            autoFocus
            aria-label="Search timezones"
            placeholder="Search UTC offset or place…"
            value={search}
            disabled={busy}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape" || e.key === "Tab") return;
              // Keep typing out of the menu's typeahead; hand arrows to its roving focus.
              e.stopPropagation();
              if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                e.preventDefault();
                const items = options.current?.querySelectorAll<HTMLElement>(
                  '[role="menuitemradio"]:not([data-disabled])',
                );
                const item = e.key === "ArrowDown" ? items?.[0] : items?.[items.length - 1];
                item?.focus();
              } else if (e.key === "Enter" && filtered.length === 1) {
                e.preventDefault();
                void select(filtered[0].timeZone);
              }
            }}
          />
        </div>
        <DropdownMenuRadioGroup
          ref={options}
          className="timezone-options"
          aria-label="Timezones"
          value={selectedOffset}
          onValueChange={(offset) => {
            const zone = timeZones.find((option) => option.offsetLabel === offset);
            if (zone) void select(zone.timeZone);
          }}
        >
          {filtered.map((zone) => (
            <DropdownMenuRadioItem
              key={zone.offsetLabel}
              value={zone.offsetLabel}
              disabled={busy}
              onSelect={(e) => e.preventDefault()}
              onFocus={(e) => e.currentTarget.scrollIntoView({ block: "nearest" })}
            >
              <span className="timezone-option-offset">{zone.offsetLabel}</span>
              <span className="timezone-option-places">{zone.places.join(", ")}</span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
        {!filtered.length && <p className="timezone-hint">No timezones found.</p>}
        <p className="timezone-hint" role="status">
          {busy ? "Saving timezone…" : "↑ ↓ to navigate · Enter to select"}
        </p>
        {error && (
          <p className="timezone-error" role="alert">
            {error}
          </p>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

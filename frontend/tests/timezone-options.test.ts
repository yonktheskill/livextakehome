import { describe, expect, it } from "vitest";
import { timezoneOptions, timezoneSearch } from "../lib/timezone";

const winter = new Date("2026-01-15T12:00:00Z");
const summer = new Date("2026-07-15T12:00:00Z");

describe("timezone offset groups", () => {
  it("has one row per offset, sorted numerically from UTC-12 to UTC+14", () => {
    const options = timezoneOptions("UTC", winter);
    const offsets = options.map((option) => option.offsetMinutes);
    expect(new Set(offsets).size).toBe(offsets.length);
    expect(offsets).toEqual([...offsets].sort((a, b) => a - b));
    expect(options[0].offsetLabel).toBe("UTC-12:00");
    expect(options.at(-1)?.offsetLabel).toBe("UTC+14:00");
  });

  it("groups Hong Kong, Beijing, and Singapore into a single UTC+08 row", () => {
    const options = timezoneOptions("UTC", winter);
    const eastAsia = options.filter((option) => option.offsetMinutes === 480);
    expect(eastAsia).toHaveLength(1);
    expect(eastAsia[0].places).toEqual(["Hong Kong", "Beijing", "Singapore"]);
    expect(eastAsia[0].searchText).toContain(timezoneSearch("Singapore"));
    expect(eastAsia[0].searchText).toContain(timezoneSearch("China"));
    expect(eastAsia[0].searchText).toContain(timezoneSearch("UTC +8"));
    expect(eastAsia[0].searchText).toContain(timezoneSearch("Asia/Taipei"));
  });

  it("keeps fractional offsets distinct and searchable", () => {
    const options = timezoneOptions("UTC", winter);
    expect(options.find((option) => option.offsetMinutes === 330)).toMatchObject({
      offsetLabel: "UTC+05:30",
      places: expect.arrayContaining(["India", "Sri Lanka"]),
    });
    expect(options.find((option) => option.offsetMinutes === 345)).toMatchObject({
      offsetLabel: "UTC+05:45",
      places: expect.arrayContaining(["Nepal"]),
    });
    expect(options.find((option) => option.offsetMinutes === 525)?.offsetLabel).toBe("UTC+08:45");
    expect(options.find((option) => option.offsetMinutes === -570)?.offsetLabel).toBe("UTC-09:30");
  });

  it("uses current daylight-saving offsets while preserving the selected IANA region", () => {
    const january = timezoneOptions("America/Toronto", winter);
    const july = timezoneOptions("America/Toronto", summer);
    expect(january.find((option) => option.timeZone === "America/Toronto")?.offsetLabel).toBe("UTC-05:00");
    expect(july.find((option) => option.timeZone === "America/Toronto")?.offsetLabel).toBe("UTC-04:00");
    expect(january.find((option) => option.places.includes("New York"))?.places).toContain("Toronto");
    expect(july.find((option) => option.places.includes("New York"))?.places).toContain("Toronto");
    expect(july.find((option) => option.places.includes("Bogotá"))?.offsetLabel).toBe("UTC-05:00");
  });

  it("recognizes saved aliases and ignores invalid imported timezones", () => {
    expect(
      timezoneOptions("Asia/Calcutta", winter).find((option) => option.offsetMinutes === 330)?.timeZone,
    ).toBe("Asia/Calcutta");
    const invalid = timezoneOptions("Invalid/Timezone", winter);
    expect(invalid.length).toBeGreaterThan(0);
    expect(invalid.some((option) => option.timeZone === "Invalid/Timezone")).toBe(false);
  });
});

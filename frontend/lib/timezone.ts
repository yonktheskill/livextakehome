// Order within an offset favors familiar places; the picker itself sorts by offset.
const featuredPlaces = [
  ["Asia/Hong_Kong", "Hong Kong"],
  ["Asia/Shanghai", "Beijing", "China"],
  ["Asia/Singapore", "Singapore"],
  ["Asia/Taipei", "Taipei", "Taiwan"],
  ["Asia/Manila", "Manila", "Philippines"],
  ["Asia/Kuala_Lumpur", "Kuala Lumpur", "Malaysia"],
  ["Etc/GMT+12", "Baker Island"],
  ["Pacific/Pago_Pago", "American Samoa"],
  ["Pacific/Niue", "Niue"],
  ["Pacific/Honolulu", "Hawaii", "United States USA"],
  ["Pacific/Marquesas", "Marquesas Islands"],
  ["America/Anchorage", "Alaska", "United States USA"],
  ["America/Los_Angeles", "Los Angeles", "United States USA"],
  ["America/Vancouver", "Vancouver", "Canada"],
  ["America/Denver", "Denver", "United States USA"],
  ["America/Phoenix", "Phoenix", "United States USA"],
  ["America/Chicago", "Chicago", "United States USA"],
  ["America/Mexico_City", "Mexico City"],
  ["America/Guatemala", "Guatemala City"],
  ["America/New_York", "New York", "United States USA"],
  ["America/Toronto", "Toronto", "Canada"],
  ["America/Bogota", "Bogotá", "Colombia"],
  ["America/Lima", "Lima", "Peru"],
  ["America/Caracas", "Caracas", "Venezuela"],
  ["America/Halifax", "Halifax", "Canada"],
  ["America/Puerto_Rico", "Puerto Rico"],
  ["America/St_Johns", "Newfoundland", "Canada"],
  ["America/Sao_Paulo", "São Paulo", "Brazil"],
  ["America/Argentina/Buenos_Aires", "Buenos Aires", "Argentina"],
  ["America/Santiago", "Santiago", "Chile"],
  ["America/Noronha", "Fernando de Noronha", "Brazil"],
  ["Atlantic/Azores", "Azores", "Portugal"],
  ["Atlantic/Cape_Verde", "Cape Verde"],
  ["UTC", "Coordinated Universal Time"],
  ["Europe/London", "London", "United Kingdom UK"],
  ["Europe/Lisbon", "Lisbon", "Portugal"],
  ["Atlantic/Reykjavik", "Reykjavík", "Iceland"],
  ["Africa/Accra", "Accra", "Ghana"],
  ["Europe/Paris", "Paris", "France"],
  ["Europe/Berlin", "Berlin", "Germany"],
  ["Europe/Madrid", "Madrid", "Spain"],
  ["Europe/Rome", "Rome", "Italy"],
  ["Africa/Lagos", "Lagos", "Nigeria"],
  ["Africa/Cairo", "Cairo", "Egypt"],
  ["Africa/Johannesburg", "Johannesburg", "South Africa"],
  ["Europe/Athens", "Athens", "Greece"],
  ["Europe/Helsinki", "Helsinki", "Finland"],
  ["Europe/Istanbul", "Istanbul", "Turkey Türkiye"],
  ["Europe/Moscow", "Moscow", "Russia"],
  ["Asia/Riyadh", "Riyadh", "Saudi Arabia"],
  ["Africa/Nairobi", "Nairobi", "Kenya"],
  ["Asia/Tehran", "Tehran", "Iran"],
  ["Asia/Dubai", "Dubai", "United Arab Emirates UAE"],
  ["Asia/Muscat", "Muscat", "Oman"],
  ["Asia/Baku", "Baku", "Azerbaijan"],
  ["Asia/Kabul", "Kabul", "Afghanistan"],
  ["Asia/Karachi", "Pakistan"],
  ["Asia/Tashkent", "Tashkent", "Uzbekistan"],
  ["Indian/Maldives", "Maldives"],
  ["Asia/Kolkata", "India", "Mumbai New Delhi"],
  ["Asia/Colombo", "Sri Lanka"],
  ["Asia/Kathmandu", "Nepal"],
  ["Asia/Dhaka", "Bangladesh"],
  ["Asia/Thimphu", "Bhutan"],
  ["Asia/Yangon", "Myanmar"],
  ["Asia/Bangkok", "Bangkok", "Thailand"],
  ["Asia/Jakarta", "Jakarta", "Indonesia"],
  ["Asia/Ho_Chi_Minh", "Ho Chi Minh City", "Vietnam"],
  ["Australia/Eucla", "Eucla"],
  ["Asia/Tokyo", "Tokyo", "Japan"],
  ["Asia/Seoul", "Seoul", "South Korea"],
  ["Australia/Darwin", "Darwin"],
  ["Australia/Adelaide", "Adelaide"],
  ["Australia/Brisbane", "Brisbane"],
  ["Australia/Sydney", "Sydney"],
  ["Pacific/Guam", "Guam"],
  ["Australia/Lord_Howe", "Lord Howe Island"],
  ["Pacific/Noumea", "New Caledonia"],
  ["Pacific/Guadalcanal", "Solomon Islands"],
  ["Pacific/Auckland", "New Zealand"],
  ["Pacific/Fiji", "Fiji"],
  ["Pacific/Tarawa", "Kiribati"],
  ["Pacific/Chatham", "Chatham Islands"],
  ["Pacific/Apia", "Samoa"],
  ["Pacific/Tongatapu", "Tonga"],
  ["Pacific/Kiritimati", "Kiritimati", "Line Islands Kiribati"],
] as const;

export type TimezoneOption = {
  offsetMinutes: number;
  offsetLabel: string;
  places: string[];
  timeZone: string;
  searchText: string;
};

export function utcOffset(timeZone: string, date: Date): number | null {
  try {
    const name = new Intl.DateTimeFormat("en-US", {
      timeZone,
      timeZoneName: "longOffset",
    })
      .formatToParts(date)
      .find((part) => part.type === "timeZoneName")?.value;
    if (name === "GMT") return 0;
    const match = name?.match(/^GMT([+-])(\d{2}):(\d{2})$/);
    return match ? (Number(match[2]) * 60 + Number(match[3])) * (match[1] === "-" ? -1 : 1) : null;
  } catch {
    return null;
  }
}

export function offsetLabel(offsetMinutes: number) {
  const minutes = Math.abs(offsetMinutes);
  return `UTC${offsetMinutes < 0 ? "-" : "+"}${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
}

export function timezoneSearch(text: string) {
  return text
    .normalize("NFD")
    .replace(/[\u0300-\u036f_\s]/g, "")
    .toLowerCase();
}

export function timezoneOptions(current: string, date: Date): TimezoneOption[] {
  const featured = new Map<string, readonly string[]>(
    featuredPlaces.map(([zone, ...names]) => [zone, names]),
  );
  const zones = new Set([...featured.keys(), ...Intl.supportedValuesOf("timeZone"), current]);
  const groups = new Map<number, TimezoneOption>();
  for (const zone of zones) {
    const offset = utcOffset(zone, date);
    if (offset === null) continue;
    const place = featured.get(zone)?.[0] ?? zone.split("/").at(-1)!.replaceAll("_", " ");
    let group = groups.get(offset);
    if (!group) {
      const label = offsetLabel(offset);
      const shortOffset = label.replace(/([+-])0/, "$1").replace(/:00$/, "");
      group = {
        offsetMinutes: offset,
        offsetLabel: label,
        places: [],
        timeZone: zone,
        searchText: `${label} ${shortOffset}`,
      };
      groups.set(offset, group);
    }
    if (group.places.length < 3 && !group.places.includes(place)) group.places.push(place);
    group.searchText += ` ${zone} ${featured.get(zone)?.join(" ") ?? place}`;
    // Keep the user's actual IANA zone and its daylight-saving rules when reselecting its group.
    if (zone === current) group.timeZone = current;
  }
  return [...groups.values()]
    .sort((a, b) => a.offsetMinutes - b.offsetMinutes)
    .map((group) => ({ ...group, searchText: timezoneSearch(group.searchText) }));
}

export function messageTime(timestamp: string, timeZone: string) {
  const date = new Date(timestamp);
  const options = { hour: "numeric", minute: "2-digit" } as const;
  try {
    return date.toLocaleTimeString([], { ...options, timeZone });
  } catch {
    // A bad imported timezone must not prevent the user opening the picker to correct it.
    return date.toLocaleTimeString([], { ...options, timeZone: "UTC" });
  }
}

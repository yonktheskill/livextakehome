import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ActionReceipt } from "../components/chat-content";
import type { Receipt } from "../lib/use-conversation";

const receipt: Receipt = {
  action_id: "booking-action",
  kind: "create_booking",
  summary: "Cal.com returned an action result; inspect its status.",
  details: {
    title: "30 min meeting between Hong Xu Tan and hong xu",
    status: "accepted",
    start: "2026-11-06T04:00:00.000Z",
    end: "2026-11-06T04:40:00.000Z",
    booking_uid: "4NE7fpJpMF4xcBgfe6iRKr",
    internal_payload: "Do not display arbitrary tool data",
  },
};

describe("booking receipt", () => {
  it("keeps booking details and formats both times in the user's saved timezone", () => {
    render(<ActionReceipt receipt={receipt} timeZone="Asia/Singapore" />);

    expect(screen.getByRole("article")).toHaveTextContent("create booking");
    expect(screen.getByText(receipt.summary)).toBeInTheDocument();
    expect(screen.getByText("30 min meeting between Hong Xu Tan and hong xu")).toBeInTheDocument();
    expect(screen.getByText("accepted")).toBeInTheDocument();
    expect(screen.getByText("4NE7fpJpMF4xcBgfe6iRKr")).toBeInTheDocument();
    expect(screen.getByText("Friday, 06-11-2026, 12:00 Asia/Singapore")).toBeInTheDocument();
    expect(screen.getByText("Friday, 06-11-2026, 12:40 Asia/Singapore")).toBeInTheDocument();
    expect(screen.queryByText("2026-11-06T04:00:00.000Z")).not.toBeInTheDocument();
    expect(screen.queryByText("Do not display arbitrary tool data")).not.toBeInTheDocument();
  });

  it.each([
    ["America/New_York", "Thursday, 05-11-2026, 23:00 America/New_York"],
    ["Asia/Kolkata", "Friday, 06-11-2026, 09:30 Asia/Kolkata"],
    ["UTC", "Friday, 06-11-2026, 04:00 UTC"],
    ["Invalid/Timezone", "Friday, 06-11-2026, 04:00 UTC"],
  ])("converts the date and time for %s", (timeZone, expected) => {
    render(<ActionReceipt receipt={receipt} timeZone={timeZone} />);
    expect(screen.getByText(expected)).toBeInTheDocument();
  });

  it("applies daylight saving offsets at each booking timestamp", () => {
    render(
      <ActionReceipt
        receipt={{
          ...receipt,
          details: { start: "2026-03-08T06:30:00Z", end: "2026-03-08T07:30:00Z" },
        }}
        timeZone="America/New_York"
      />,
    );
    expect(screen.getByText("Sunday, 08-03-2026, 01:30 America/New_York")).toBeInTheDocument();
    expect(screen.getByText("Sunday, 08-03-2026, 03:30 America/New_York")).toBeInTheDocument();
  });

  it("renders midnight as 00:00 and tolerates unavailable timestamps", () => {
    render(
      <ActionReceipt
        receipt={{ ...receipt, details: { start: "2026-11-06T00:00:00Z", end: "Unavailable" } }}
        timeZone="UTC"
      />,
    );
    expect(screen.getByText("Friday, 06-11-2026, 00:00 UTC")).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
  });
});

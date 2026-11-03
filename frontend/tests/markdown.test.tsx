import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SafeMarkdown } from "../components/markdown";
describe("safe assistant content", () => {
  it("renders GFM tables and safe external links", () => {
    render(
      <SafeMarkdown
        content={"| Meeting | Time |\n| --- | --- |\n| Intro | 10:00 UTC |\n\n[Details](https://cal.com/example)"}
      />,
    );
    expect(screen.getByRole("table")).toHaveTextContent("10:00 UTC");
    expect(screen.getByRole("link")).toHaveAttribute("rel", "noopener noreferrer");
  });
  it("disables raw HTML, image requests, unsafe links, and arbitrary relative URLs", () => {
    const { container } = render(
      <SafeMarkdown
        content={
          '<script>alert(1)</script>\n\n<img src="https://tracker.test/pixel">\n\n![pixel](https://tracker.test/a)\n\n[unsafe](javascript:alert(1))\n\n[relative](/api/auth/logout)'
        }
      />,
    );
    expect(container.querySelector("script,img,iframe")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });
});

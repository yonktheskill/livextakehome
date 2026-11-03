"use client";

import { Button, Input } from "@headlessui/react";
import { Eye, EyeOff } from "lucide-react";
import { useId, useState, type ComponentPropsWithoutRef } from "react";

type SecretInputProps = Omit<ComponentPropsWithoutRef<"input">, "type"> & {
  visibilityLabel: string;
};

export function SecretInput({ visibilityLabel, id, className = "", ...props }: SecretInputProps) {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const [visible, setVisible] = useState(false);
  return (
    <div className="secret-input">
      <Input {...props} id={inputId} className={`input ${className}`} type={visible ? "text" : "password"} />
      <Button
        type="button"
        className="icon-button secret-input-toggle"
        aria-label={`${visible ? "Hide" : "Show"} ${visibilityLabel}`}
        aria-controls={inputId}
        aria-pressed={visible}
        disabled={props.disabled}
        onClick={() => setVisible((value) => !value)}
      >
        {visible ? <EyeOff size={18} aria-hidden="true" /> : <Eye size={18} aria-hidden="true" />}
      </Button>
    </div>
  );
}

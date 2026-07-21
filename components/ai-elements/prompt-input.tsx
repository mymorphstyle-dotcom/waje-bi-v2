"use client";

import { cn } from "@/lib/utils";
import { CornerDownLeftIcon, LoaderCircleIcon } from "lucide-react";
import type {
  ComponentProps,
  FormEvent,
  HTMLAttributes,
  KeyboardEvent,
} from "react";
import { useState } from "react";

export type PromptInputMessage = {
  text: string;
  files: [];
};

export type PromptInputProps = Omit<ComponentProps<"form">, "onSubmit"> & {
  onSubmit: (
    message: PromptInputMessage,
    event: FormEvent<HTMLFormElement>,
  ) => void | Promise<void>;
};

export function PromptInput({ className, onSubmit, ...props }: PromptInputProps) {
  return (
    <form
      className={cn("border bg-background", className)}
      onSubmit={(event) => {
        event.preventDefault();
        const formData = new FormData(event.currentTarget);
        void onSubmit({ text: String(formData.get("message") ?? ""), files: [] }, event);
      }}
      {...props}
    />
  );
}

export type PromptInputTextareaProps = ComponentProps<"textarea">;

export function PromptInputTextarea({
  className,
  onCompositionEnd,
  onCompositionStart,
  onKeyDown,
  ...props
}: PromptInputTextareaProps) {
  const [composing, setComposing] = useState(false);

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    onKeyDown?.(event);
    if (
      event.defaultPrevented
      || event.key !== "Enter"
      || event.shiftKey
      || composing
      || event.nativeEvent.isComposing
    ) return;
    event.preventDefault();
    const submit = event.currentTarget.form?.querySelector<HTMLButtonElement>(
      'button[type="submit"]',
    );
    if (!submit?.disabled) event.currentTarget.form?.requestSubmit();
  }

  return (
    <textarea
      className={cn("max-h-48 min-h-16 w-full resize-none", className)}
      name="message"
      onCompositionEnd={(event) => {
        setComposing(false);
        onCompositionEnd?.(event);
      }}
      onCompositionStart={(event) => {
        setComposing(true);
        onCompositionStart?.(event);
      }}
      onKeyDown={handleKeyDown}
      {...props}
    />
  );
}

export type PromptInputFooterProps = HTMLAttributes<HTMLDivElement>;

export function PromptInputFooter({ className, ...props }: PromptInputFooterProps) {
  return (
    <div
      className={cn("flex items-center justify-between gap-2 px-2 pb-1", className)}
      {...props}
    />
  );
}

export type PromptInputToolsProps = HTMLAttributes<HTMLDivElement>;

export function PromptInputTools({ className, ...props }: PromptInputToolsProps) {
  return <div className={cn("flex min-w-0 items-center", className)} {...props} />;
}

export type PromptInputSubmitProps = ComponentProps<"button"> & {
  status?: "submitted" | "streaming" | "ready" | "error";
};

export function PromptInputSubmit({
  children,
  className,
  status,
  ...props
}: PromptInputSubmitProps) {
  const pending = status === "submitted" || status === "streaming";
  return (
    <button
      className={cn(
        "grid size-9 shrink-0 place-items-center rounded-xl bg-primary text-primary-foreground disabled:opacity-45",
        className,
      )}
      type="submit"
      {...props}
    >
      {children ?? (pending
        ? <LoaderCircleIcon aria-hidden="true" className="size-4 animate-spin" />
        : <CornerDownLeftIcon aria-hidden="true" className="size-4" />)}
    </button>
  );
}

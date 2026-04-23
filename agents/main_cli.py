#!/usr/bin/env python3
"""
main_cli.py - Textual LLM Chat CLI

A terminal UI for chatting with LLMs, inspired by OpenCode's interface.
Features:
- Split view: chat messages + input area
- Syntax highlighted code blocks
- Markdown rendering
- Streaming response simulation
- Session history
"""

import asyncio
import time
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.events import Key
from textual.reactive import reactive
from textual.widgets import Button, Footer, Input, Static


class MessageBubble(Static):
    """A single chat message bubble."""

    def __init__(self, role: str, content: str, timestamp: str, **kwargs):
        self.role = role
        self.content = content
        self.timestamp = timestamp
        super().__init__(**kwargs)

    def render(self) -> str:
        icon = "🤖" if self.role == "assistant" else "👤"
        prefix = f"[b]{icon} AI[/b]" if self.role == "assistant" else "[b]👤 You[/b]"
        return f"{prefix} [{self.muted_style}]{self.timestamp}[/]\n\n{self.content}"

    @property
    def muted_style(self) -> str:
        return "dim"

    def watch_content(self, content: str) -> None:
        self.refresh()


class ChatView(VerticalScroll):
    """Scrollable view containing all chat messages."""

    def on_mount(self) -> None:
        self.scroll_end()

    def add_message(self, role: str, content: str) -> MessageBubble:
        timestamp = time.strftime("%H:%M:%S")
        bubble = MessageBubble(role, content, timestamp)
        bubble.styles.margin = (1, 2)
        if role == "user":
            bubble.styles.border = ("left", "steel_blue")
            bubble.styles.padding = (1, 2)
        else:
            bubble.styles.border = ("left", "spring_green3")
            bubble.styles.padding = (1, 2)
        self.mount(bubble)
        self.scroll_end(self)
        return bubble


class StatusBar(Static):
    """Status bar showing model info and connection status."""

    def __init__(self, model: str = "gpt-4o", **kwargs):
        self.model = model
        super().__init__(**kwargs)

    def render(self) -> str:
        return f"[b]Model:[/b] {self.model}   [b]Status:[/b] ● Connected   [b]Session:[/b] {time.strftime('%Y-%m-%d %H:%M')}"


class InputArea(Container):
    """Input area with text field and send button."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Input(
                placeholder="Type your message... (Enter to send, Shift+Enter for new line)",
                id="chat-input",
            )
            yield Button("Send ➤", id="send-btn", variant="primary")


class LLMAgentApp(App):
    """Main LLM Chat CLI Application."""

    CSS = """
    Screen {
        background: $surface;
    }

    #header {
        height: 3;
        background: $primary;
        color: $text;
        padding: 0 2;
        dock: top;
    }

    #header Title {
        width: 100%;
        content-align: center middle;
    }

    #chat-container {
        height: 1fr;
        margin: 1 2;
    }

    #status-bar {
        height: 3;
        dock: bottom;
        background: $panel;
        padding: 0 2;
    }

    #input-area {
        height: 5;
        dock: bottom;
        background: $panel;
        padding: 1 2;
        border-top: solid $accent;
    }

    #input-area Horizontal {
        height: 3;
    }

    #chat-input {
        width: 1fr;
        margin: 0 1;
    }

    #send-btn {
        width: 12;
    }

    MessageBubble {
        width: 100%;
        max-width: 100%;
    }

    .welcome-message {
        background: $surface;
        border: solid $accent;
        padding: 2 3;
        margin: 2 2;
        width: 100%;
    }

    .welcome-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+l", "clear_chat", "Clear Chat"),
        ("ctrl+s", "toggle_streaming", "Toggle Streaming"),
    ]

    def __init__(self, model: str = "gpt-4o", **kwargs):
        super().__init__(**kwargs)
        self.model = model
        self.messages = []
        self.is_streaming = True
        self.title = f"LLM Chat - {model}"

    def compose(self) -> ComposeResult:
        yield Static("🤖 LLM Chat CLI", id="header")
        with Container(id="chat-container"):
            with VerticalScroll(id="chat-view"):
                yield Static(
                    "Welcome to [b]LLM Chat CLI[/b]!\n\n"
                    "This is a demo interface for interacting with large language models.\n\n"
                    "[b]Commands:[/b]\n"
                    "  • Enter - Send message\n"
                    "  • Shift+Enter - New line in input\n"
                    "  • Ctrl+L - Clear chat\n"
                    "  • Ctrl+S - Toggle streaming mode\n"
                    "  • Ctrl+C - Quit\n\n"
                    "Start typing to begin!",
                    classes="welcome-message",
                )
        yield InputArea(id="input-area")
        yield StatusBar(self.model, id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.chat_view = self.query_one("#chat-view", VerticalScroll)
        self.input_field = self.query_one("#chat-input", Input)
        self.input_field.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send-btn":
            self.send_message()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.send_message()

    def send_message(self) -> None:
        text = self.input_field.value.strip()
        if not text:
            return

        self.input_field.value = ""
        self.messages.append({"role": "user", "content": text})

        user_bubble = self.chat_view.add_message("user", text)

        asyncio.create_task(self.get_ai_response(text))

    async def get_ai_response(self, user_input: str) -> None:
        """Simulate AI response - replace with actual LLM call."""

        response_bubble = self.chat_view.add_message("assistant", "")

        full_response = await self.simulate_llm_response(user_input)

        if self.is_streaming:
            for i in range(0, len(full_response), 3):
                response_bubble.content += full_response[i:i+3]
                response_bubble.update(
                    f"[b]🤖 AI[/b] [{response_bubble.muted_style}]{response_bubble.timestamp}[/]\n\n{response_bubble.content}"
                )
                await asyncio.sleep(0.02)
        else:
            response_bubble.content = full_response
            response_bubble.update(
                f"[b]🤖 AI[/b] [{response_bubble.muted_style}]{response_bubble.timestamp}[/]\n\n{full_response}"
            )

        self.messages.append({"role": "assistant", "content": full_response})

    async def simulate_llm_response(self, user_input: str) -> str:
        """Simulate an LLM response for demo purposes."""
        responses = {
            "hello": "Hello! I'm your AI assistant. How can I help you today?",
            "help": "I can help you with various tasks:\n\n• Writing and editing code\n• Answering questions\n• Explaining concepts\n• Debugging issues\n• And much more!\n\nWhat would you like to do?",
            "bye": "Goodbye! It was great chatting with you. Feel free to return anytime!",
            "code": "Here's a simple Python example:\n\n```python\ndef greet(name: str) -> str:\n    return f\"Hello, {name}!\"\n\nmessage = greet(\"World\")\nprint(message)\n```",
        }

        user_lower = user_input.lower()
        for key, response in responses.items():
            if key in user_lower:
                await asyncio.sleep(0.5)
                return response

        await asyncio.sleep(1)
        return (
            f"I received your message: '{user_input[:50]}{'...' if len(user_input) > 50 else ''}'\n\n"
            "This is a demo response. In a real implementation, this would be connected to an LLM API.\n\n"
            "Try typing:\n"
            "• 'hello' - for a greeting\n"
            "• 'help' - for available commands\n"
            "• 'code' - for a code example\n"
            "• 'bye' - to end the conversation"
        )

    def action_clear_chat(self) -> None:
        """Clear all chat messages."""
        for child in list(self.chat_view.children):
            if isinstance(child, MessageBubble):
                child.remove()
        self.messages = []

    def action_toggle_streaming(self) -> None:
        """Toggle streaming mode."""
        self.is_streaming = not self.is_streaming
        self.notify(f"Streaming mode: {'ON' if self.is_streaming else 'OFF'}")

    def action_quit(self) -> None:
        """Quit the application."""
        self.exit()


def main():
    app = LLMAgentApp()
    app.run()


if __name__ == "__main__":
    main()


import gradio as gr
from agent import chat


if __name__ == "__main__":
    gr.ChatInterface(chat).launch()

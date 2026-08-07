"""Open a headed persistent Chromium session for the first X login."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config import Settings  # noqa: E402


async def main() -> None:
    settings = Settings.load()
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(settings.x_profile_dir),
            channel="msedge",
            headless=False,
            locale="pt-BR",
            viewport={"width": 1440, "height": 1000},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(
            f"{settings.x_base_url}/i/flow/login",
            wait_until="domcontentloaded",
        )
        print("\nFaça login manualmente no X na janela aberta.")
        print("Conclua 2FA/CAPTCHA, confirme que a página inicial abriu e volte aqui.")
        await asyncio.to_thread(
            input, "Pressione ENTER para salvar a sessão e fechar... "
        )
        await context.close()
        print(f"Sessão persistida em: {settings.x_profile_dir}")


if __name__ == "__main__":
    asyncio.run(main())

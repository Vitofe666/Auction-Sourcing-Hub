import asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI()

class TargetURL(BaseModel):
    url: str

# Render needs a simple health check to confirm the server successfully started
@app.get("/")
async def health_check():
    return {"status": "healthy", "service": "jewelry-scraper"}

@app.post("/scrape")
async def scrape_auction(target: TargetURL):
    async with async_playwright() as p:
        try:
            # Launch Chromium in headless mode with sandboxing disabled for container security
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            page = await browser.new_page()

            # Navigate to the auction URL
            await page.goto(target.url, wait_until="networkidle", timeout=60000)

            # Extract Text Details
            title = await page.locator("h1.lot-title, .lot-details__title").first.inner_text()
            description = await page.locator(".lot-description-text, .lot-details__description").first.inner_text()

            # Extract Image URLs
            img_elements = await page.locator("img.lot-image, .gallery-thumbnail img").all()
            image_urls = []

            for img in img_elements:
                src = await img.get_attribute("src")
                if src:
                    clean_url = src.split('?')[0]
                    if not clean_url.startswith("http"):
                        clean_url = "https:" + clean_url if clean_url.startswith("//") else "https://www.thesaleroom.com" + clean_url
                    image_urls.append(clean_url)

            await browser.close()

            return {
                "title": title.strip(),
                "description": description.strip(),
                "images": list(set(image_urls))
            }

        except Exception as e:
            if 'browser' in locals():
                await browser.close()
            raise HTTPException(status_code=500, detail=f"Scraping failed: {str(e)}")

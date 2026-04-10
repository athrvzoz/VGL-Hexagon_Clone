import os
import random
from playwright.sync_api import sync_playwright

def handle_task_action(page, action="approve"):
    """
    Clicks the approve or decline button on the side panel.
    Also handles the alert dialog that pops up.
    """
    # Auto-accept the browser alert that appears upon clicking
    page.once("dialog", lambda dialog: dialog.accept())
    
    if action.lower() == "approve":
        print(f"   -> Clicking 'Approve' button...")
        page.locator(".sp-action:has-text('Approve')").click()
    elif action.lower() in ["decline", "reject"]:
        print(f"   -> Clicking 'Decline' button...")
        page.locator(".sp-action:has-text('Decline')").click()

def run(playwright):
    # Launch browser in non-headless mode to see the actions
    browser = playwright.chromium.launch(headless=False, slow_mo=500)
    
    # Create context with downloads enabled
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()

    print("1. Navigating to http://localhost:4200/")
    page.goto("http://localhost:4200/")

    # Wait for the table rows to appear
    page.wait_for_selector(".data-table tbody tr")

    print("2. Clicking on the first task in C2 project...")
    # Click the first row in the table
    page.locator(".data-table tbody tr").first.click()

    print("2b. Claiming the Task...")
    # Click the 'Claim Task' button in the side panel
    page.locator(".sp-action:has-text('Claim Task')").click()
    page.wait_for_timeout(500) # Small wait for state update

    print("3a. In Task section, clicking on 'Export Data' option...")
    # Wait for the download to start when clicking Export Data
    with page.expect_download() as download_info:
        page.locator(".sp-action:has-text('Export Data')").click()
    
    download = download_info.value
    export_path = os.path.join(os.getcwd(), download.suggested_filename)
    download.save_as(export_path)
    print(f"   ✓ Exported data saved to: {export_path}")

    print("3b. Going to FILES tab...")
    # Click the FILES tab in the side panel
    page.locator(".sp-tab:has-text('FILES')").click()

    print("3c. Downloading the file from the Attachments section...")
    # New selector matching the updated app.html where attachments are in .sp-action
    with page.expect_download() as download_info2:
        page.locator(".sp-body .sp-action").first.click()
    
    download2 = download_info2.value
    attachment_path = os.path.join(os.getcwd(), f"attachment_{download2.suggested_filename}")
    download2.save_as(attachment_path)
    print(f"   ✓ Attachment saved to: {attachment_path}")

    print("4. Switching back to TASK tab to Create Transmittal...")
    page.locator(".sp-tab:has-text('TASK')").click()

    # Click 'Create Transmittal'
    page.locator(".sp-action:has-text('Create Transmittal')").click()
    page.wait_for_selector(".modal-container")
    
    # Handle the alert that appears when 'FINISH' is clicked
    page.once("dialog", lambda dialog: dialog.accept())
    
    print("   -> Clicking 'FINISH' in transmittal modal...")
    page.locator(".modal-btn.primary:has-text('FINISH')").click()
    
    print("5. Navigating to 'Personal Claimed Tasks' view...")
    page.locator(".nav-item:has-text('Personal Claimed Tasks')").click()
    
    print("   -> Staying in Personal Claimed Tasks for 10 seconds...")
    page.wait_for_timeout(10000)

    print("6. Reselecting the task in the Claimed view...")
    # Select the task again (it should be in the claimed list now)
    page.locator(".data-table tbody tr").first.click()

    # Ensure we are in TASK tab
    page.locator(".sp-tab:has-text('TASK')").click()

    # Randomly choose between approve or decline
    chosen_action = random.choice(["approve", "decline"])
    print(f"7. Randomly determined final action: {chosen_action.upper()}")
    handle_task_action(page, action=chosen_action)

    print("Automation complete! Closing browser.")
    context.close()
    browser.close()

if __name__ == "__main__":
    with sync_playwright() as playwright:
        run(playwright)

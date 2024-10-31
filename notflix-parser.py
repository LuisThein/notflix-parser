import imaplib
import email
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from email.header import decode_header
import time
from bs4 import BeautifulSoup
import os

# Account credentials
USERNAME = os.environ.get("NOTFLIX_EMAIL")
PASSWORD = os.environ.get("NOTFLIX_PASSWORT")
IMAP_SERVER = os.environ.get("IMAP_SERVER")

TARGET_SUBJECT = "Important: How to update your Netflix Household"

# Set up the WebDriver (Chrome in this case)
#driver_path = "/usr/local/bin/chromedriver"  # Specify your WebDriver path here 
options = webdriver.ChromeOptions()
options.add_argument("--headless=new") # remove to see the browser open
driver = webdriver.Chrome(options=options)

def process_important_email(body):
    """
    This function processes the HTML email body to extract the URL from the specified button.
    """
    print("Processing important email with specified subject...")

    # Parse HTML content with BeautifulSoup
    soup = BeautifulSoup(body, "html.parser")

    # Find the <a> tag with class "h5" and text "Yes, This Was Me"
    button = soup.find("a", class_="h5", string="Yes, This Was Me")

    # Extract the href attribute if the button is found
    if button:
        button_link = button.get("href")
        print("Button link found:", button_link)

        # Use Selenium to navigate to the button link
        driver.get(button_link)

        # Wait for the page to load
        time.sleep(5)  # You might want to adjust this time based on page load speed

        try:
            # Locate the button by its data-uia attribute and click it
            confirm_button = driver.find_element(By.CSS_SELECTOR, 'button[data-uia="set-primary-location-action"]')

            # Scroll to the button if needed
            ActionChains(driver).move_to_element(confirm_button).perform()
            confirm_button.click()

            print("Button clicked successfully.")
        except Exception as e:
            print("Failed to locate or click the confirmation button:", e)
    else:
        print("No relevant button link found in the email.")

def fetch_unread_emails():
    # Connect to the server and go to its inbox
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(USERNAME, PASSWORD)
    mail.select("inbox")

    # Search for all unread emails
    status, messages = mail.search(None, 'UNSEEN')

    # Convert the result into a list of email IDs
    email_ids = messages[0].split()

    # Loop over each email
    for email_id in email_ids:
        # Fetch the email by ID
        status, msg_data = mail.fetch(email_id, "(RFC822)")
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                # Parse a bytes email into a message object
                msg = email.message_from_bytes(response_part[1])

                # Decode the email subject
                subject, encoding = decode_header(msg["Subject"])[0]
                if isinstance(subject, bytes):
                    # If it's a bytes type, decode to str
                    subject = subject.decode(encoding if encoding else "utf-8")
                print("Subject:", subject)

                # Check if the email subject matches the target subject
                if subject == TARGET_SUBJECT:
                    print("Target email found!")

                    # If the email is multipart, extract the HTML part
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            content_type = part.get_content_type()
                            content_disposition = str(part.get("Content-Disposition"))
                            try:
                                # Only process HTML parts
                                if content_type == "text/html" and "attachment" not in content_disposition:
                                    body = part.get_payload(decode=True).decode()
                                    break  # Stop after finding the first HTML part
                            except Exception as e:
                                print(f"Could not decode body: {e}")
                    else:
                        # If it's not multipart, just get the payload
                        body = msg.get_payload(decode=True).decode()

                    # Pass the HTML body to the processing function
                    process_important_email(body)

        # Mark the mail as seen
        mail.store(email_id, '+FLAGS', '\\Seen')

    # Close the connection and logout
    mail.close()
    mail.logout()

# Check for new emails every minute
while True:
    print("Checking for new emails...")
    try:
        fetch_unread_emails()
    except:
        print("Something went wron retrying in 60s")
    time.sleep(60)
import asyncio
import inspect
import traceback
from typing import Annotated

from hercules.core.playwright_manager import PlaywrightManager
from hercules.telemetry import EventData, EventType, add_event
from hercules.utils.dom_helper import get_element_outer_html
from hercules.utils.dom_mutation_observer import subscribe  # type: ignore
from hercules.utils.dom_mutation_observer import unsubscribe  # type: ignore
from hercules.utils.logger import logger
from hercules.utils.ui_messagetype import MessageType
from playwright.async_api import ElementHandle, Page


async def click(
    selector: Annotated[
        str,
        "The properly formed query selector string to identify the element for the click action (e.g. [mmid='114']). When \"mmid\" attribute is present, use it for the query selector.",
    ],
    wait_before_execution: Annotated[
        float,
        "Optional wait time in seconds before executing the click event logic.",
        float,
    ] = 0.0,
) -> Annotated[str, "A message indicating success or failure of the click."]:
    """
    Executes a click action on the element matching the given query selector string within the currently open web page.
    If there is no page open, it will raise a ValueError. An optional wait time can be specified before executing the click logic. Use this to wait for the page to load especially when the last action caused the DOM/Page to load.

    Parameters:
    - selector: The query selector string to identify the element for the click action.
    - wait_before_execution: Optional wait time in seconds before executing the click event logic. Defaults to 0.0 seconds.

    Returns:
    - Success if the click was successful, Appropropriate error message otherwise.
    """
    logger.info(f'Executing ClickElement with "{selector}" as the selector')
    add_event(EventType.INTERACTION, EventData(detail="click"))
    # Initialize PlaywrightManager and get the active browser page
    browser_manager = PlaywrightManager()
    page = await browser_manager.get_current_page()

    if page is None:  # type: ignore
        raise ValueError("No active page found. OpenURL command opens a new page.")

    function_name = inspect.currentframe().f_code.co_name  # type: ignore

    await browser_manager.take_screenshots(f"{function_name}_start", page)

    await browser_manager.highlight_element(selector, True)

    dom_changes_detected = None

    def detect_dom_changes(changes: str):  # type: ignore
        nonlocal dom_changes_detected
        dom_changes_detected = changes  # type: ignore

    subscribe(detect_dom_changes)
    result = await do_click(page, selector, wait_before_execution)
    await asyncio.sleep(0.5)  # sleep for 100ms to allow the mutation observer to detect changes
    unsubscribe(detect_dom_changes)
    await browser_manager.take_screenshots(f"{function_name}_end", page)
    await browser_manager.notify_user(result["summary_message"], message_type=MessageType.ACTION)

    if dom_changes_detected:
        return f"Success: {result['summary_message']}.\n As a consequence of this action, new elements have appeared in view: {dom_changes_detected}. This means that the action to click {selector} is not yet executed and needs further interaction. Get all_fields DOM to complete the interaction."
    return result["detailed_message"]


async def do_click(page: Page, selector: str, wait_before_execution: float) -> dict[str, str]:
    """
    Executes the click action on the element with the given selector within the provided page,
    including searching within iframes if necessary.

    Parameters:
    - page: The Playwright page instance.
    - selector: The query selector string to identify the element for the click action.
    - wait_before_execution: Optional wait time in seconds before executing the click event logic.

    Returns:
    dict[str,str] - Explanation of the outcome of this operation represented as a dictionary with 'summary_message' and 'detailed_message'.
    """
    logger.info(f'Executing ClickElement with "{selector}" as the selector. Wait time before execution: {wait_before_execution} seconds.')

    # Wait before execution if specified
    if wait_before_execution > 0:
        await asyncio.sleep(wait_before_execution)

    # Function to search for the element, including within iframes
    async def find_element_within_iframes():
        # Check for the element on the main page
        element = await page.query_selector(selector)
        if element:
            return element

        # If not found, iterate over all iframes and search
        for frame in page.frames:
            element = await frame.query_selector(selector)
            if element:
                return element
        return None

    # Wait for the selector to be present and ensure it's attached and visible. If timeout, try JavaScript click
    try:
        logger.info(f'Executing ClickElement with "{selector}" as the selector. Waiting for the element to be attached and visible.')

        # Attempt to find the element on the main page or in iframes
        element = await find_element_within_iframes()
        if element is None:
            raise ValueError(f'Element with selector: "{selector}" not found')

        logger.info(f'Element with selector: "{selector}" is attached. Scrolling it into view if needed.')
        try:
            await element.scroll_into_view_if_needed(timeout=200)
            logger.info(f'Element with selector: "{selector}" is attached and scrolled into view. Waiting for the element to be visible.')
        except Exception:
            # If scrollIntoView fails, just move on, not a big deal
            pass

        try:
            await element.wait_for_element_state("visible", timeout=200)
            logger.info(f'Executing ClickElement with "{selector}" as the selector. Element is attached and visible. Clicking the element.')
        except Exception:
            # If the element is not visible, try to click it anyway
            pass

        element_tag_name = await element.evaluate("element => element.tagName.toLowerCase()")
        element_outer_html = await get_element_outer_html(element, page, element_tag_name)

        if element_tag_name == "option":
            element_value = await element.get_attribute("value")
            parent_element = await element.evaluate_handle("element => element.parentNode")
            await parent_element.select_option(value=element_value)  # type: ignore

            logger.info(f'Select menu option "{element_value}" selected')

            return {
                "summary_message": f'Select menu option "{element_value}" selected',
                "detailed_message": f'Select menu option "{element_value}" selected. The select element\'s outer HTML is: {element_outer_html}.',
            }

        msg = await perform_javascript_click(page, selector)
        return {"summary_message": msg, "detailed_message": f"{msg} The clicked element's outer HTML is: {element_outer_html}."}  # type: ignore
    except Exception as e:
        logger.error(f'Unable to click element with selector: "{selector}". Error: {e}')
        traceback.print_exc()
        msg = f'Unable to click element with selector: "{selector}" since the selector is invalid. Proceed by retrieving DOM again.'
        return {"summary_message": msg, "detailed_message": f"{msg}. Error: {e}"}


async def is_element_present(page: Page, selector: str) -> bool:
    """
    Checks if an element is present on the page, either in the regular DOM or inside a shadow DOM.

    Parameters:
    - page: The Playwright page instance.
    - selector: The query selector string to identify the element.

    Returns:
    - True if the element is present, False otherwise.
    """
    element = await page.query_selector(selector)

    # If the element is found in the regular DOM, return True
    if element is not None:
        return True

    # If not found in the regular DOM, check inside shadow DOMs
    is_in_shadow_dom = await page.evaluate(
        """
            (selector) => {
                const findElementInShadowDOMAndIframes = (parent, selector) => {
                    // Try to find the element in the current context
                    let element = parent.querySelector(selector);
                    if (element) {
                        return element; // Element found in the current context
                    }

                    // Search inside shadow DOMs and iframes
                    const elements = parent.querySelectorAll('*');
                    for (const el of elements) {
                        // Search inside shadow DOMs
                        if (el.shadowRoot) {
                            element = findElementInShadowDOMAndIframes(el.shadowRoot, selector);
                            if (element) {
                                return element; // Element found in shadow DOM
                            }
                        }
                        // Search inside iframes
                        if (el.tagName.toLowerCase() === 'iframe') {
                            let iframeDocument;
                            try {
                                // Access the iframe's document if it's same-origin
                                iframeDocument = el.contentDocument || el.contentWindow.document;
                            } catch (e) {
                                // Cannot access cross-origin iframe; skip to the next element
                                continue;
                            }
                            if (iframeDocument) {
                                element = findElementInShadowDOMAndIframes(iframeDocument, selector);
                                if (element) {
                                    return element; // Element found inside iframe
                                }
                            }
                        }
                    }
                    return null; // Element not found
                };
                return findElementInShadowDOMAndIframes(document, selector) !== null;
            }

    """,
        selector,
    )

    return is_in_shadow_dom


async def perform_playwright_click(element: ElementHandle, selector: str):
    """
    Performs a click action on the element using Playwright's click method.

    Parameters:
    - element: The Playwright ElementHandle instance representing the element to be clicked.
    - selector: The query selector string of the element.

    Returns:
    - None
    """
    logger.info(f"Performing first Step: Playwright Click on element with selector: {selector}")
    await element.click(force=True, timeout=200)


async def perform_javascript_click(page: Page, selector: str):
    """
    Performs a click action on the element using JavaScript.

    Parameters:
    - page: The Playwright page instance.
    - selector: The query selector string of the element.

    Returns:
    - None
    """
    js_code = """(selector) => {
                // Helper function to search for an element in regular DOM, shadow DOMs, and iframes
                const findElementInShadowDOMAndIframes = (parent, selector) => {
                    // First, try to find the element in the current DOM context (either document or shadowRoot)
                    let element = parent.querySelector(selector);
                    
                    if (element) {
                        return element; // Element found in the current context
                    }
                    
                    // If not found, look inside shadow roots and iframes of elements in this context
                    const elements = parent.querySelectorAll('*');
                    for (const el of elements) {
                        // Search inside shadow DOMs
                        if (el.shadowRoot) {
                            element = findElementInShadowDOMAndIframes(el.shadowRoot, selector);
                            if (element) {
                                return element; // Element found in shadow DOM
                            }
                        }
                        // Search inside iframes
                        if (el.tagName.toLowerCase() === 'iframe') {
                            let iframeDocument;
                            try {
                                // Access the iframe's document if it's same-origin
                                iframeDocument = el.contentDocument || el.contentWindow.document;
                            } catch (e) {
                                // Cannot access cross-origin iframe; skip to the next element
                                continue;
                            }
                            if (iframeDocument) {
                                element = findElementInShadowDOMAndIframes(iframeDocument, selector);
                                if (element) {
                                    return element; // Element found inside iframe
                                }
                            }
                        }
                    }
                    
                    return null; // Element not found
                };

                // Start by searching in the regular document (DOM)
                let element = findElementInShadowDOMAndIframes(document, selector);

                if (!element) {
                    console.log(`perform_javascript_click: Element with selector ${selector} not found`);
                    return `perform_javascript_click: Element with selector ${selector} not found`;
                }

                if (element.tagName.toLowerCase() === "option") {
                    let value = element.text;
                    let parent = element.parentElement;

                    parent.value = element.value; // Directly set the value if possible
                    // Trigger change event if necessary
                    let event = new Event('change', { bubbles: true });
                    parent.dispatchEvent(event);

                    console.log("Select menu option", value, "selected");
                    return "Select menu option: " + value + " selected";
                } else {
                    console.log("About to click selector", selector);
                    // If the element is a link, make it open in the same tab
                    if (element.tagName.toLowerCase() === "a") {
                        element.target = "_self";
                    }
                    let ariaExpandedBeforeClick = element.getAttribute('aria-expanded');
                    element.click();
                    let ariaExpandedAfterClick = element.getAttribute('aria-expanded');
                    if (ariaExpandedBeforeClick === 'false' && ariaExpandedAfterClick === 'true') {
                        return "Executed JavaScript Click on element with selector: " + selector + ". Very important: As a consequence, a menu has appeared where you may need to make further selection. Very important: Get all_fields DOM to complete the action.";
                    }
                    return "Executed JavaScript Click on element with selector: " + selector;
                }
            }

    """
    try:
        logger.info(f"Executing JavaScript click on element with selector: {selector}")
        result: str = await page.evaluate(js_code, selector)
        logger.debug(f"Executed JavaScript Click on element with selector: {selector}")
        return result
    except Exception as e:
        logger.error(f"Error executing JavaScript click on element with selector: {selector}. Error: {e}")
        traceback.print_exc()
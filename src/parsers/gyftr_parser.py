"""
Parser for GYFTR voucher emails.
"""
import re
from bs4 import BeautifulSoup

# Mapping of GyFTR logo IDs to Brand Names
# Used as fallback when alt text is missing
BRAND_ID_MAP = {
    '344': 'Swiggy Money Voucher',
    '72': 'Myntra',
    '510': 'Amazon Shopping Voucher',
    '22': 'Flipkart Gift Card', # Common ID, adding proactively
    '14': 'Dominos Pizza',      # Common ID
    '19': 'Baskin Robbins',     # Common ID
    '25': 'KFC',                # Common ID
    '26': 'Pizza Hut',          # Common ID
    '1669891154334_1canbt2f4olb4y2t26': 'Amazon Pay Gift Card', # Amazon Pay
}

# Field Normalization Mapping
# Maps various email labels to standardized Sheet Column Headers
FIELD_MAPPING = {
    # Establish "Code" as the master key for all codes
    'Promo Code': 'Code',
    'Daily Objects Promo Code': 'Code',
    'Gift Voucher Code': 'Code',
    'E-Voucher Code': 'Code',
    'Gift Card Code': 'Code',
    'Voucher Code': 'Code',
    'E-Gift Card Code': 'Code',
    
    # Values
    'Gift Voucher Value': 'Value',
    'Voucher Value': 'Value',
    'Gift Card Value': 'Value',
    
    # Pins
    'Gift Voucher Pin': 'Pin',
    'Voucher Pin': 'Pin',
    'Gift Card Pin': 'Pin',
    'PIN': 'Pin', 
    'Pin': 'Pin',
    
    # Dates
    'Valid Until': 'Expiry',
    'Expiry Date': 'Expiry',
    'Expiration Date': 'Expiry',
    'Valid Till': 'Expiry',
}

def extract_vouchers_from_html(html):
    """
    Extracts voucher details from GYFTR email HTML.
    Returns a list of dictionaries, where each dictionary represents a voucher.
    """
    if not html:
        print("⚠️ Warning: Empty HTML content passed to parser.")
        return []

    try:
        soup = BeautifulSoup(html, 'html.parser')
        vouchers = []
        
        # Strategy:
        # 1. Find the "Brand" cell. In the samples, this is a <td> with width="100px" 
        #    containing a brand logo/name.
        # 2. The details are in the immediate next sibling <td> (often width="370px").
        # 3. Inside the details <td>, we look for Label/Value pairs.
        
        # Find all potential brand cells
        brand_cells = soup.find_all('td', width="100px")
        
        if not brand_cells:
            # Fallback for updated email templates?
            # Could log this for debugging if it happens often.
            pass

        for brand_cell in brand_cells:
            # Verify it looks like a brand cell (contains an image or specific text div)
            # The brand name is usually in a div under the img
            brand_div = brand_cell.find('div', style=lambda s: s and 'text-align:center' in s)
            
            # Fallback: check for img alt text if div not found or empty
            brand_name = "Unknown Brand"
            logo_url = ""
            img = brand_cell.find('img')
            
            if img and img.get('src'):
                logo_url = img.get('src')

            if brand_div:
                brand_text = brand_div.get_text(strip=True)
                if brand_text:
                    brand_name = brand_text
            elif img and img.get('alt'):
                brand_name = img.get('alt')
            
            # Second Fallback: Try to identify brand from logo URL
            if brand_name == "Unknown Brand" and logo_url:
                # Pattern 1: Standard logos .../logo/123.png...
                match = re.search(r'/logo/(\d+)\.png', logo_url)
                if match:
                    logo_id = match.group(1)
                    if logo_id in BRAND_ID_MAP:
                        brand_name = BRAND_ID_MAP[logo_id]
                    else:
                        brand_name = f"Unknown Brand (ID: {logo_id})"
                
                # Pattern 2: Brand images .../brands/filename.png
                if brand_name == "Unknown Brand":
                    match = re.search(r'/brands/([^/]+)\.png', logo_url)
                    if match:
                        logo_id = match.group(1)
                        if logo_id in BRAND_ID_MAP:
                            brand_name = BRAND_ID_MAP[logo_id]
                        else:
                             # Keep filename as last resort fallback
                            brand_name = f"Unknown Brand ({logo_id})"
            
            # If we still don't have a meaningful brand name, skip (might not be a voucher row)
            if not brand_name:
                continue

            # Find the details cell (sibling)
            details_cell = brand_cell.find_next_sibling('td')
            if not details_cell:
                print(f"⚠️ Warning: Found brand '{brand_name}' but no details cell found next to it.")
                continue
                
            voucher_data = {'Brand': brand_name}
            if logo_url:
                voucher_data['Logo'] = f'=IMAGE("{logo_url}")'
            
            # Extract key-value pairs from details_cell
            # Pattern: 
            # Label: <div style="... font-size: 11px ...">Label</div>
            # Value: <div style="... font-size: 13px; font-weight: bold ...">Value</div>
            # We look for the Label, then take the next sibling div as Value.
            
            # We search for all divs that might be labels
            potential_labels = details_cell.find_all('div')
            
            found_at_least_one_field = False
            for i, div in enumerate(potential_labels):

                style = div.get('style', '').lower()
                text = div.get_text(strip=True)
                
                # Heuristic for Label: font-size: 11px (or similar small size)
                # And it shouldn't be empty
                if 'font-size: 11px' in style or 'font-size:11px' in style:
                    if not text: 
                        continue
                        
                    key = text.replace(':', '').strip()

                    # Normalize key using our mapping (merging Promo Code -> E-Gift Card Code)
                    if key in FIELD_MAPPING:
                        key = FIELD_MAPPING[key]
                    
                    # The value is usually the next sibling element (div)
                    # We can't just use potential_labels[i+1] because the list is flattened 
                    # and might skip structure. We should use DOM traversal.
                    value_div = div.find_next_sibling('div')
                    
                    if value_div:
                        value = value_div.get_text(strip=True)
                        # Clean up value
                        voucher_data[key] = value
                        found_at_least_one_field = True
            
            # Only add if we found some data
            if len(voucher_data) > 1 and found_at_least_one_field:
                vouchers.append(voucher_data)
            elif brand_name != "Unknown Brand":
                 # If we found a brand but no details, it might be a false positive or layout change
                 print(f"⚠️ Warning: Found brand '{brand_name}' but no details could be extracted. Check HTML layout.")
            
        return vouchers

    except Exception as e:
        print(f"❌ Critical Error in HTML Parser: {e}")
        return []


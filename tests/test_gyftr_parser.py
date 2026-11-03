import unittest
from unittest.mock import patch

from src.parsers.gyftr_parser import extract_vouchers_from_html


class GyftrParserTests(unittest.TestCase):
    def test_parser_execution_error_is_not_an_empty_voucher_result(self):
        with patch(
            'src.parsers.gyftr_parser.BeautifulSoup',
            side_effect=RuntimeError('Synthetic parser failure'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'Synthetic parser failure'):
                extract_vouchers_from_html('<p>Test voucher email</p>')

    def test_legacy_amazon_table_with_multiple_vouchers(self):
        html = """
        <p>Thank you for buying Amazon Gift Code from Gyftr via HDFC Bank SmartBuy.</p>
        <table><tbody>
          <tr><th>QR Code</th><th>E-Gift Card Code</th><th>Face Value</th>
              <th>GCID</th><th>Expiry Date</th></tr>
          <tr><td><img alt="EXAMPLE-CODE-1"></td><td>EXAMPLE-CODE-1</td>
              <td>1000.00</td><td>REFERENCE-1</td><td>08 Feb 2026</td></tr>
          <tr><td></td><td>EXAMPLE-CODE-2</td><td>500.00</td>
              <td>REFERENCE-2</td><td>08 Feb 2026</td></tr>
        </tbody></table>
        """
        vouchers = extract_vouchers_from_html(html)
        self.assertEqual(len(vouchers), 2)
        self.assertEqual(vouchers[0], {
            'Brand': 'Amazon Gift Code', 'Code': 'EXAMPLE-CODE-1',
            'Value': '1000.00', 'GCID': 'REFERENCE-1', 'Expiry': '08 Feb 2026',
        })
        self.assertEqual(vouchers[1]['Code'], 'EXAMPLE-CODE-2')
        self.assertNotIn('QR Code', vouchers[0])

    def test_legacy_pin_preserves_leading_zeroes(self):
        html = """
        <p>Thank you for buying Flipkart Gift Voucher from Gyftr.</p>
        <table>
          <tr><td>E-Gift Card Code</td><td>Face Value</td><td>PIN</td><td>Expiry Date</td></tr>
          <tr><td>001234567890</td><td>500.00</td><td>001234</td><td>15 Oct 2025</td></tr>
        </table>
        """
        voucher = extract_vouchers_from_html(html)[0]
        self.assertEqual(voucher['Code'], '001234567890')
        self.assertEqual(voucher['Pin'], '001234')

    def test_legacy_promo_table_without_face_value(self):
        html = """
        <p>Please find your Myntra Promo Code from Gyftr via HDFC Bank SmartBuy.</p>
        <table>
          <tr><td>QR Code</td><td>Promo Code</td><td>Expiry Date</td></tr>
          <tr><td></td><td>EXAMPLE-PROMO</td><td>31 Aug 2024</td></tr>
        </table>
        """
        vouchers = extract_vouchers_from_html(html)
        self.assertEqual(vouchers, [{
            'Brand': 'Myntra Promo Code', 'Code': 'EXAMPLE-PROMO',
            'Expiry': '31 Aug 2024',
        }])

    def test_legacy_table_inside_layout_table_is_not_duplicated(self):
        html = """
        <p>Thank you for buying Swiggy Money Voucher voucher from Gyftr.</p>
        <table><tr><td><table>
          <tr><td>Voucher Code</td><td>Face Value</td><td>Expiry Date</td></tr>
          <tr><td>EXAMPLE-SWIGGY</td><td>1000.00</td><td>31 Aug 2025</td></tr>
        </table></td></tr></table>
        """
        vouchers = extract_vouchers_from_html(html)
        self.assertEqual(len(vouchers), 1)
        self.assertEqual(vouchers[0]['Code'], 'EXAMPLE-SWIGGY')

    def test_legacy_labeled_makemytrip_vouchers(self):
        html = """
        <p>We thank you for Shopping at GyFTR for MakeMyTrip Hotel e-Pay voucher .</p>
        <p>Pls find below the Voucher details:</p>
        <div>Voucher Code: <b>0012345678901234</b>
             Face Value: 5000.00 Valid Till: 28 Oct 2024</div>
        <div>Voucher Code: <b>0012345678901235</b>
             Face Value: 5000.00 Valid Till: 28 Oct 2024</div>
        """
        vouchers = extract_vouchers_from_html(html)
        self.assertEqual(len(vouchers), 2)
        self.assertEqual(vouchers[0], {
            'Brand': 'MakeMyTrip Hotel e-Pay voucher',
            'Code': '0012345678901234', 'Value': '5000.00', 'Expiry': '28 Oct 2024',
        })

    def test_modern_template_is_unchanged(self):
        html = """
        <table><tr>
          <td width="100px"><img src="https://example.com/logo/510.png">
              <div style="text-align:center">Amazon Shopping Voucher</div></td>
          <td>
            <div style="font-size: 11px">Gift Voucher Code</div><div>EXAMPLE-AMAZON</div>
            <div style="font-size: 11px">Gift Voucher Value</div><div>1000</div>
            <div style="font-size: 11px">Valid Till</div><div>31 Oct 2027</div>
          </td>
        </tr></table>
        """
        self.assertEqual(extract_vouchers_from_html(html), [{
            'Brand': 'Amazon Shopping Voucher',
            'Logo': '=IMAGE("https://example.com/logo/510.png")',
            'Code': 'EXAMPLE-AMAZON', 'Value': '1000', 'Expiry': '31 Oct 2027',
        }])

    def test_claim_invitation_is_not_a_brand_voucher(self):
        html = """
        <p>Claim the Voucher of your choice from the below mentioned brands.</p>
        <p>Your Unique Password: EXAMPLE-PASSWORD</p>
        <p>Click here to claim your Gift Voucher.</p>
        """
        self.assertEqual(extract_vouchers_from_html(html), [])

    def test_legacy_brand_is_identified_from_issuer_terms(self):
        html = """
        <p>Please find your details below:</p>
        <table>
          <tr><td>E-Gift Card Code</td><td>Face Value</td><td>PIN</td><td>Expiry Date</td></tr>
          <tr><td>EXAMPLE-MYNTRA</td><td>250</td><td>0123</td><td>Mar 07, 2019</td></tr>
        </table>
        <p>Terms &amp; Conditions 1.Myntra E- Gift Card is issued by QwikCilver.</p>
        """
        self.assertEqual(extract_vouchers_from_html(html)[0]['Brand'], 'Myntra')

    def test_legacy_header_without_data_is_not_a_voucher(self):
        html = """
        <table><tr><td>Voucher Code</td><td>Face Value</td><td>Expiry Date</td></tr></table>
        """
        self.assertEqual(extract_vouchers_from_html(html), [])


if __name__ == '__main__':
    unittest.main()

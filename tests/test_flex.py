import xml.etree.ElementTree as ET

from thetagang.flex import _extract_cash_transactions, _extract_statement_info


def test_flex_cash_transaction_parsing() -> None:
    xml = """
    <FlexQueryResponse>
      <FlexStatements>
        <FlexStatement accountId="U123" fromDate="20240101" toDate="20240131">
          <CashTransactions>
            <CashTransaction accountId="U123" currency="USD" amount="5.00"
              type="Dividends" description="DIV" tradeDate="20240115"
              conid="123" symbol="AAA"/>
          </CashTransactions>
          <Dividends>
            <Dividend accountId="U123" currency="USD" amount="2.00"
              description="DIV2" date="20240116"/>
          </Dividends>
        </FlexStatement>
      </FlexStatements>
    </FlexQueryResponse>
    """
    root = ET.fromstring(xml)
    statement = _extract_statement_info(root)
    assert statement["account_id"] == "U123"
    assert statement["from_date"] == "20240101"
    assert statement["to_date"] == "20240131"

    transactions = _extract_cash_transactions(root, ["CashTransactions", "Dividends"])
    assert len(transactions) == 2
    amounts = sorted([t["amount"] for t in transactions if t["amount"] is not None])
    assert amounts == [2.0, 5.0]


def test_flex_cash_transaction_account_filter() -> None:
    xml = """
    <FlexQueryResponse>
      <FlexStatements>
        <FlexStatement accountId="U123">
          <CashTransactions>
            <CashTransaction accountId="U123" currency="USD" amount="5.00"/>
            <CashTransaction accountId="U999" currency="USD" amount="7.00"/>
          </CashTransactions>
        </FlexStatement>
      </FlexStatements>
    </FlexQueryResponse>
    """
    root = ET.fromstring(xml)
    transactions = _extract_cash_transactions(
        root, ["CashTransactions"], account_id_filter="U123"
    )
    assert len(transactions) == 1
    assert transactions[0]["amount"] == 5.0

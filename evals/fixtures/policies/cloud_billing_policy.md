# Cloud Infrastructure Billing & Overage Terms

Applies to the company's Amazon Web Services accounts. Last revised 2026-04-15.

## 1. Billing Cycle
Usage is metered continuously and invoiced monthly in arrears. The billing period runs from
00:00 UTC on the first day of the month to 23:59 UTC on the last day. Charges appear on the
invoice issued in the first week of the following month.

## 2. Free Tier
New accounts receive 12 months of limited free-tier usage. Free-tier allowances do not roll
over between months. Usage beyond the allowance is billed at standard on-demand rates with
no warning, which is the most common cause of an unexpected first invoice.

## 3. NAT Gateway Charges
NAT Gateways are billed on two separate dimensions: an hourly charge for each gateway that
exists, and a **data processing charge per GB** passed through it. The data processing
charge applies to all traffic, including traffic to and from services inside AWS. This is
the single most frequent source of unexplained cost increases, because moving data between
private subnets and S3 or ECR without a VPC endpoint routes through the NAT Gateway and is
billed per GB. **Internal budget alert threshold for NAT Gateway data processing is USD 200
per month.** Spend above this must be reviewed by the infrastructure lead.

## 4. EC2 Instance Hours
On-demand instances are billed per second with a 60-second minimum. Instances that are
stopped are not billed for compute, but attached EBS volumes continue to be billed.

## 5. S3 Storage
Standard storage is billed per GB-month, prorated. Requests, data transfer out, and
lifecycle transitions are billed separately from storage.

## 6. Tax
Sales tax is applied at the rate applicable to the billing address on file. For US accounts
this is currently 8.5%. Tax is calculated on the subtotal after any credits are applied.

## 7. Disputes
Billing disputes must be raised within 60 days of the invoice date. Include the invoice
number and the specific line item in question.

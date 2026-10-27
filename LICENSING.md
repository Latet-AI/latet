# Licensing

Latet is source-available: the code is public and you can read, check and change it. The Latet
Community Edition is free for everyone, companies of any size included. The Latet Enterprise Edition
adds what a company needs to manage Latet centrally, under a commercial license.

The legal terms are the [Latet Sustainable Use License](LICENSE) for the Community Edition and the
[Latet Enterprise License](ee/LICENSE) for the files in `ee` directories. This page says the same in
plain words; where the two differ, the licenses win.

## Free, for everyone

- **At work, in any company.** Use Latet for your company's own work, on any number of computers
  and servers, for any number of people: free, whatever the company's size or revenue.
- **For yourself.** Personal projects, study, research, trying it out: free.
- **Non-commercial use** of any kind, by nonprofits, schools and universities too.
- **Changing it.** You may change Latet for those same uses, and share it or your changes free of
  charge for non-commercial purposes. Keep the `LICENSE` file and the copyright notices, and say
  that you changed it.

## What needs a commercial license

Write to [info@latet.ai](mailto:info@latet.ai) before you do any of these:

- **Selling Latet**, or a changed copy of it.
- **Offering it to others as a service**: running Latet for other companies or for your customers,
  hosted or managed.
- **Building it into a product you sell**: a product or service for others that includes Latet or
  uses it underneath.
- **A commercial fork**: a product made from Latet's code and sold, licensed or offered for a fee.

Some examples:

- A company runs Latet for its 5,000 employees and its build servers: free.
- A consultant installs and configures Latet for a company, which then uses it for its own work: free
  for the company, and the consultant may charge for the work.
- An agency runs Latet in front of the AI tools it sells to its clients: commercial license.
- A security vendor ships Latet inside its own product: commercial license.

## The Enterprise Edition

The files in `ee` directories are the Enterprise Edition. Using them in production needs a valid
Latet Enterprise license; developing and testing with them is free. Write to
[info@latet.ai](mailto:info@latet.ai) with your company's name and how many people will use Latet.

## Good to know

- **No calls home.** The Community Edition has no license keys and never contacts anyone to check
  anything.
- **Contributing.** Contributions are accepted under the [contributor license agreement](CLA.md):
  you keep your copyright and allow your contribution to be part of Latet under every license it is
  offered under, the commercial ones included.
- **The Python client.** `sdk/python` (`latet-client`) is under the MIT license, in its own
  [LICENSE](sdk/python/LICENSE) file; the rest of this repository keeps the licenses above.
- **Third-party software** in Latet keeps its own licenses, listed in
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The optional models Latet downloads on request
  (name detection, text recognition) come with their own licenses too, shown when you install them.

# Mailbox fixtures

The unit tests build sparse, ordinary-file stand-ins under pytest's temporary
directory for DMI, PCI configuration space, `/dev/mem`, and `/dev/port`. The
mailbox port fixture models indexed reads in memory, so tests never access host
device nodes.

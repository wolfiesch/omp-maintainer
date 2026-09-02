"""github-broker: PAT-holding companion service for OMP Maintainer.

OMP Maintainer container holds zero credentials; every GitHub side-effect (REST +
git clone/fetch/push) flows through this service over an HMAC-authenticated
internal channel. See `omp_maintainer.proxy.server` for the request surface.
"""

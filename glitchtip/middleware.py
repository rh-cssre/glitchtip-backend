import abc
import io
import logging
import zlib

import brotli
from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponseForbidden  # For returning error response

try:
    import uwsgi

    has_uwsgi = True
except ImportError:
    has_uwsgi = False


logger = logging.getLogger(__name__)

# --- Configuration Constants ---

# Default chunk size in bytes for reading the *compressed* input stream.
# Used by decompressor base class unless overridden by subclasses. 8KB is common.
DEFAULT_CHUNK_SIZE = 1024 * 8

# Chunk size specifically for reading zlib-based (gzip, deflate) compressed streams.
Z_CHUNK = 1024 * 8

# Chunk size specifically for reading Brotli ('br') compressed streams.
BR_CHUNK = 1024 * 8

# --- uWSGI Chunked Input Handling ---
if has_uwsgi:
    # Provides a file-like interface for reading chunked input via uWSGI API
    class UWsgiChunkedInput(io.RawIOBase):
        def __init__(self):
            self._internal_buffer = b""

        def readable(self):
            return True

        def readinto(self, buf):
            if not self._internal_buffer:
                try:
                    self._internal_buffer = uwsgi.chunked_read()
                except OSError as e:
                    logger.error("uwsgi.chunked_read() failed: %s", e)
                    self._internal_buffer = b""
            n = min(len(buf), len(self._internal_buffer))
            if n > 0:
                buf[:n] = self._internal_buffer[:n]
                self._internal_buffer = self._internal_buffer[n:]
            return n


class StreamingDecompressorBase(io.RawIOBase):
    """
    Abstract Base Class for streaming HTTP content decoders (gzip, deflate, brotli).
    Handles common logic like reading chunks, filling output buffer, and checking size limits.
    Subclasses must implement the actual decompression logic.
    """

    CHUNK_SIZE = DEFAULT_CHUNK_SIZE  # Default chunk size for reading compressed data

    def __init__(self, fp):
        self.fp = fp  # The underlying (compressed) file-like object
        self.total_decompressed = 0  # Track total decompressed bytes accurately
        self.decompressor = None  # Decompressor object (library specific)
        self.eof_reached = False  # Flag if EOF on compressed stream is hit
        self.internal_buffer = (
            b""  # Buffer for decompressed data not yet read by caller
        )

    def readable(self):
        return True

    @abc.abstractmethod
    def _init_decompressor(self):
        """Initialize and return the specific decompressor object."""
        raise NotImplementedError

    @abc.abstractmethod
    def _decompress_chunk(self, chunk):
        """Decompress a chunk of compressed data, return decompressed bytes."""
        raise NotImplementedError

    @abc.abstractmethod
    def _flush_decompressor(self):
        """Flush the decompressor at EOF, return any remaining bytes."""
        raise NotImplementedError

    def _read_compressed_chunk(self):
        """Reads a chunk from the underlying compressed stream using the class's CHUNK_SIZE."""
        return self.fp.read(self.CHUNK_SIZE)

    def readinto(self, buf):
        """
        Reads from the compressed stream, decompresses data incrementally into buf,
        and checks against the decompressed size limit.
        """
        if self.decompressor is None:
            try:
                self.decompressor = self._init_decompressor()
            except Exception as e:
                logger.error("Failed to initialize decompressor: %s", e, exc_info=True)
                raise IOError(f"Failed to initialize decompressor: {e}") from e

        n = 0  # Total bytes written into buf in this call
        max_length = len(buf)  # Max bytes requested by caller

        while max_length > 0:
            # If we have data in our internal buffer, use that first
            if self.internal_buffer:
                read_size = min(max_length, len(self.internal_buffer))
                buf[n : n + read_size] = self.internal_buffer[:read_size]
                self.internal_buffer = self.internal_buffer[read_size:]
                n += read_size
                max_length -= read_size
                continue  # Check if more space in buf needs filling

            # If EOF reached and buffer is empty, we're done
            if self.eof_reached:
                break

            # Read next chunk of compressed data
            chunk = self._read_compressed_chunk()

            decompressed_bytes = b""
            if chunk:
                # Decompress the chunk
                try:
                    decompressed_bytes = self._decompress_chunk(chunk)
                except (zlib.error, brotli.error) as e:
                    logger.warning(
                        "%s decompression error: %s", self.__class__.__name__, e
                    )
                    return n  # Return bytes processed so far before the error
            else:
                # EOF reached on input stream
                self.eof_reached = True
                try:
                    # Flush the decompressor
                    decompressed_bytes = self._flush_decompressor()
                except (zlib.error, brotli.error) as e:
                    logger.warning(
                        "%s error during final flush: %s", self.__class__.__name__, e
                    )
                    return n  # Return bytes processed so far

            # If decompression yielded data, process it
            if decompressed_bytes:
                # Calculate potential new total size FIRST
                potential_new_total = self.total_decompressed + len(decompressed_bytes)

                # Check size limit BEFORE adding to buffer or returning
                if potential_new_total > settings.GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE:
                    logger.warning(
                        "RequestDataTooBig: %s decompressed size (%d) would exceed limit (%d)",
                        self.__class__.__name__,
                        potential_new_total,
                        settings.GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE,
                    )
                    raise RequestDataTooBig(
                        f"Decompressed size exceeded limit of "
                        f"{settings.GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE} bytes"
                    )

                # Size limit not exceeded, update total and add to buffer
                self.total_decompressed = potential_new_total
                self.internal_buffer += decompressed_bytes

                # Now that buffer has data, loop again to copy it to buf
                continue

            # If EOF was reached and flush yielded no data, break
            elif self.eof_reached:
                break

        return n  # Return total bytes written to buf in this call


class ZDecoder(StreamingDecompressorBase):
    """Decompressor for zlib-based streams (deflate, gzip)."""

    CHUNK_SIZE = Z_CHUNK  # Use specific chunk size for Zlib

    def __init__(self, fp, z_obj=None):
        super().__init__(fp)
        self._init_z_obj = z_obj  # Store pre-configured obj (e.g., for gzip)
        self.unconsumed_tail = b""
        # Only allow retry for raw deflate if obj wasn't pre-configured (i.e., for deflate, not gzip)
        self.retry_allowed = z_obj is None

    def _init_decompressor(self):
        # Use pre-configured object if provided (for gzip), else default (for deflate)
        return self._init_z_obj if self._init_z_obj else zlib.decompressobj()

    def _decompress_chunk(self, chunk):
        # Handles unconsumed tail and potential retry for raw deflate
        compressed = self.unconsumed_tail + chunk
        try:
            decompressed = self.decompressor.decompress(compressed)
            self.unconsumed_tail = self.decompressor.unconsumed_tail
            return decompressed
        except zlib.error as e:
            if not self.retry_allowed:
                logger.warning("Zlib decompression error (no retry): %s", e)
                raise
            # Retry with raw deflate window bits
            logger.debug(
                "Retrying zlib decompression with raw deflate wbits due to error: %s", e
            )
            self.decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
            self.retry_allowed = False  # Only retry once
            decompressed = self.decompressor.decompress(compressed)
            self.unconsumed_tail = self.decompressor.unconsumed_tail
            return decompressed

    def _flush_decompressor(self):
        return self.decompressor.flush()


class BrotliDecoder(StreamingDecompressorBase):
    """Decompressor for Brotli streams ('br' encoding)."""

    CHUNK_SIZE = BR_CHUNK  # Use specific chunk size for Brotli

    def _init_decompressor(self):
        return brotli.Decompressor()

    def _decompress_chunk(self, chunk):
        # Brotli process allows setting a buffer limit, to prevent memory spikes
        # Without this, we can see near 1GB memory spikes for a single request.
        max_allowed_size = settings.GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE
        max_chunk_output = max_allowed_size - self.total_decompressed
        if max_chunk_output < 0:
            max_chunk_output = 0

        # Brotli process() handles internal state/buffering
        return self.decompressor.process(chunk, output_buffer_limit=max_chunk_output)

    def _flush_decompressor(self):
        # Brotli requires processing empty bytes at EOF to flush
        # The base class handles calling _decompress_chunk(b"") implicitly via the main loop
        # when EOF is detected, so returning empty bytes here is correct.
        return b""


class DeflateDecoder(ZDecoder):
    """Decoding for "content-encoding: deflate" """

    def __init__(self, fp):
        # Initialize ZDecoder without a pre-configured object to allow retry logic
        super().__init__(fp, None)


class GzipDecoder(ZDecoder):
    """Decoding for "content-encoding: gzip" """

    def __init__(self, fp):
        # Initialize ZDecoder with specific zlib options for gzip (disables retry)
        super().__init__(fp, zlib.decompressobj(16 + zlib.MAX_WBITS))


# --- Other Middleware Classes ---


class SetRemoteAddrFromForwardedFor(object):
    """Middleware to set REMOTE_ADDR based on X-Forwarded-For header."""

    def __init__(self, get_response=None):
        self.get_response = get_response
        if not getattr(settings, "SENTRY_USE_X_FORWARDED_FOR", True):
            from django.core.exceptions import MiddlewareNotUsed

            raise MiddlewareNotUsed

    def __call__(self, request):
        self.process_request(request)
        response = self.get_response(request)
        return response

    def _remove_port_number(self, ip_address):
        # Helper to strip port number if present
        if "[" in ip_address and "]" in ip_address:
            return ip_address[ip_address.find("[") + 1 : ip_address.find("]")]
        if "." in ip_address and ip_address.rfind(":") > ip_address.rfind("."):
            return ip_address.rsplit(":", 1)[0]
        return ip_address

    def process_request(self, request):
        try:
            real_ip = request.META["HTTP_X_FORWARDED_FOR"]
            real_ip = real_ip.split(",")[0].strip()
            real_ip = self._remove_port_number(real_ip)
            request.META["REMOTE_ADDR"] = real_ip
        except KeyError:
            pass  # Header not present


class ChunkedMiddleware(object):
    """Middleware to handle chunked transfer encoding with uWSGI."""

    def __init__(self, get_response=None):
        self.get_response = get_response
        if not has_uwsgi:
            from django.core.exceptions import MiddlewareNotUsed

            raise MiddlewareNotUsed

    def __call__(self, request):
        self.process_request(request)
        response = self.get_response(request)
        return response

    def process_request(self, request):
        # If chunked encoding is used with uWSGI, replace stream with UWsgiChunkedInput
        if request.META.get("HTTP_TRANSFER_ENCODING", "").lower() == "chunked":
            request._stream = io.BufferedReader(UWsgiChunkedInput())
            request.META["CONTENT_LENGTH"] = "4294967295"


class DecompressBodyMiddleware(object):
    """
    Middleware that decompresses request body based on Content-Encoding header
    and applies size limits using refactored Decoder classes.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        encoding = request.META.get("HTTP_CONTENT_ENCODING", "").lower()
        decoder_class = None

        # Map encoding to the refactored decoder classes
        if encoding == "gzip":
            decoder_class = GzipDecoder
        elif encoding == "deflate":
            decoder_class = DeflateDecoder
        elif encoding == "br":
            decoder_class = BrotliDecoder

        if decoder_class:
            try:
                # Wrap the original stream with the appropriate decoder
                request._stream = decoder_class(request._stream)
                # Workaround for streaming transformations: Set large dummy CONTENT_LENGTH
                # to indicate unknown length after replacing request._stream.
                request.META["CONTENT_LENGTH"] = "4294967295"
                # Remove encoding header as stream is now decompressed
                request.META.pop("HTTP_CONTENT_ENCODING", None)
            except Exception as e:
                logger.error(
                    "Error initializing request body decompressor '%s': %s",
                    encoding,
                    e,
                    exc_info=True,
                )
                return HttpResponseForbidden(
                    f"Invalid compressed request body for encoding '{encoding}': {e}",
                    status=400,
                )

        # Process the request (view will read from potentially wrapped stream)
        try:
            response = self.get_response(request)
            return response
        except RequestDataTooBig as e:
            logger.warning("RequestDataTooBig caught in middleware: %s", e)
            return HttpResponseForbidden(f"{e}", status=413)
        except (zlib.error, brotli.error) as e:
            logger.error(
                "Decompression error during view processing: %s", e, exc_info=True
            )
            return HttpResponseForbidden(
                f"Invalid compressed request body: {e}", status=400
            )
        except Exception as e:
            logger.error(
                "Unexpected error in DecompressBodyMiddleware/View: %s",
                e,
                exc_info=True,
            )
            raise


class ContentLengthHeaderMiddleware(object):
    """Ensure responses have a Content-Length header if not streaming."""

    def __init__(self, get_response=None):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        return self.process_response(request, response)

    def process_response(self, request, response):
        # If header already present or response is streaming, do nothing
        if "Transfer-Encoding" in response or "Content-Length" in response:
            return response
        if getattr(response, "streaming", False):
            return response

        # If response has content, calculate and set Content-Length
        if hasattr(response, "content"):
            try:
                response["Content-Length"] = str(len(response.content))
            except TypeError:  # Handle cases where content might not have a len
                pass
        return response

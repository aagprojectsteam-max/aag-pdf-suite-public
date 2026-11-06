# Runtime dependencies

The installer downloads these from PyPI into the app's own environment. They are not stored in this repository.

- Qt / PySide6: LGPLv3 or GPL, or commercial terms. https://doc.qt.io/qtforpython-6/licenses.html
- pikepdf: MPL-2.0. https://github.com/pikepdf/pikepdf
- libqpdf: Apache-2.0. https://github.com/qpdf/qpdf
- PyMuPDF / MuPDF: AGPLv3 or a commercial license. https://pymupdf.readthedocs.io/en/latest/about.html
- Pillow: HPND. https://github.com/python-pillow/Pillow
- NumPy: BSD-3-Clause. https://github.com/numpy/numpy
- OpenCV: Apache-2.0. https://github.com/opencv/opencv

The OCR page calls system tools that you install yourself, each under its own license: OCRmyPDF, Tesseract, Ghostscript (AGPL or commercial), Poppler utilities, and optionally qpdf, unpaper and pngquant.

This app is licensed AGPL-3.0-or-later, which matches the AGPL option of PyMuPDF.

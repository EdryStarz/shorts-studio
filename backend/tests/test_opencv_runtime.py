import cv2


def test_opencv_runtime_has_required_classifiers():
    assert cv2.__version__.startswith("4.11.")
    assert hasattr(cv2, "CascadeClassifier")
    assert hasattr(cv2, "VideoCapture")
    assert cv2.data.haarcascades

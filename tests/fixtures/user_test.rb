require "minitest/autorun"

class UserTest < Minitest::Test
  def test_save
    assert User.new(1).save
  end
end

require_relative "../lib/user"

RSpec.describe User do
  describe "#save" do
    context "when valid" do
      it "persists the record" do
        expect(User.new(1).save).to eq(true)
      end
    end
  end
end
